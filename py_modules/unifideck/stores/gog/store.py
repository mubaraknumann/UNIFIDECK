"""``GOGStore`` — public StoreBase implementation; façade over all GOG modules.

OP-22-gog-store | py_modules/unifideck/stores/gog/store.py

Top-level GOG façade implementing the
``StoreBase`` contract. Composes all the focused
modules:

* ``GOGConfig`` — paths + URLs;
* ``GOGTokenManager`` — auth tokens + gogdl creds;
* ``GOGExeResolver`` — exe location;
* ``GOGLibrary`` — owned + installed games;
* ``GOGBrowserAuth`` — OAuth browser flow;
* ``GOGInstaller`` — install pipeline;
* ``GOGDlcManager`` — DLC ops;
* ``GOGUpdatesChecker`` — update detection +
  execution.

Service injection split: when ``browser_monitor``
is provided, this is the *auth-only* instance (a
short-lived store created specifically for OAuth);
otherwise it's the *runtime* instance with the
installer + DLC + updates wired up. This avoids
the installer being constructed during auth (it
would need gogdl which isn't always available
at auth-time) and keeps the auth flow lightweight.

The ``_ensure_auth_shortcut`` flow creates a
Steam-visible shortcut pointing at the auth
launcher; users tap it from Steam UI to start the
OAuth browser dance.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from ...auth.browser import OAuthBrowserMonitor
from ...auth.edge_browser import EdgeBrowser
from ...auth.orchestrator import AuthOrchestrator
from ...core.types import (
    AuthResult,
    Events,
    Game,
    InstallResult,
    Result,
    StoreInfo,
)
from ...services.shortcut import ShortcutService
from ...utils.locale import get_unifideck_locale
from ..shared.store_base import StoreBase
from .auth import GOGBrowserAuth
from .config import GOG_AUTH_URL_FILE, GOGConfig
from .dlc import GOGDlcManager
from .exe_resolver import GOGExeResolver
from .install import GOGInstaller
from .library import GOGLibrary
from .tokens import GOGTokenManager
from .updates import GOGUpdatesChecker

if TYPE_CHECKING:
    from ...config import ConfigManager
    from ...core.cache_manager import CacheManager
    from ...event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)


class GOGStore(StoreBase):
    """GOG storefront façade — ``StoreBase`` impl composing focused modules.

    ``store_info`` class attribute drives store
    discovery + the UI's store list. ``auth_method
    = "oauth"`` triggers the browser-based auth
    flow; ``uses_wine = False`` indicates GOG
    installs are platform-native (gogdl handles
    Wine internally for Windows games).
    """

    store_info = StoreInfo(
        name="gog",
        display_name="GOG",
        auth_method="oauth",
        icon_asset="gog.png",
        uses_wine=False,
        supports_install=True,
    )

    def __init__(
        self,
        bus: EventBus,
        cache: CacheManager,
        plugin_dir: str | None = None,
        config: ConfigManager | None = None,
        browser_monitor: OAuthBrowserMonitor | None = None,
        shortcut_service: ShortcutService | None = None,
        edge_browser: EdgeBrowser | None = None
    ) -> None:
        """Wire all collaborators, split auth-only vs runtime construction.

        Construction split:

        * ``browser_monitor`` provided → build
          ``GOGBrowserAuth`` (auth-only store);
        * Otherwise → build ``GOGInstaller``,
          ``GOGDlcManager``,
          ``GOGUpdatesChecker`` (runtime store).

        The library + tokens + exe resolver are
        always built — both modes need them for
        reading installs + checking auth status.

        Args:
            bus: ``EventBus``.
            cache: ``CacheManager``.
            plugin_dir: plugin install root (for
                gogdl binary lookup).
            config: ``ConfigManager`` (for
                config-derived ``GOGConfig``).
            browser_monitor: OAuth browser
                monitor; presence triggers
                auth-only construction.
            shortcut_service: Steam shortcut
                creator (used during auth).
            edge_browser: Edge browser
                controller (used during auth).
        """
        super().__init__(bus, cache, plugin_dir, config)
        self._gog_config: GOGConfig = GOGConfig.from_config_manager(config)
        logger.info(
            "[GOGStore] %s",
            self._gog_config.describe(),
        )
        self._config_manager = config
        self._shortcut_service = shortcut_service
        self._edge = edge_browser
        self._tokens = GOGTokenManager(self._gog_config, bus=bus)
        self._exe = GOGExeResolver()

        self._library = GOGLibrary(
            config=self._gog_config,
            tokens=self._tokens,
            exe_finder=self._exe.find,
        )
        if browser_monitor is not None:
            orchestrator = AuthOrchestrator(
                bus=bus,
                browser_monitor=browser_monitor,
                store_name="gog",
            )
            self._auth: GOGBrowserAuth | None = GOGBrowserAuth(
                bus=bus,
                orchestrator=orchestrator,
                tokens=self._tokens,
                config=self._gog_config,
            )
        else:
            self._auth = None
            gogdl_bin = self._resolve_gogdl_bin()
            self._installer = GOGInstaller(
                config=self._gog_config,
                tokens=self._tokens,
                gogdl_bin=gogdl_bin,
                exe_finder=self._exe.find,
                locale_fn=lambda: get_unifideck_locale(
                    self._config_manager,
                ),
            )
            self._dlc = GOGDlcManager(
                config=self._gog_config,
                tokens=self._tokens,
                gogdl_bin=gogdl_bin,
                locale_fn=lambda: get_unifideck_locale(
                    self._config_manager,
                ),
                resolve_install_path=self._library.get_installed_game_info,
            )
            self._updates = GOGUpdatesChecker(
                config=self._gog_config,
                tokens=self._tokens,
                gogdl_bin=gogdl_bin,
                get_installed_ids=self._library.get_installed,
                resolve_install_info=self._library.get_installed_game_info,
            )

    async def is_available(self) -> bool:
        """Probe whether GOG is configured + authenticated.

        Two gates:

        1. Config validity (URLs + paths
           present);
        2. Library auth probe (tokens load + 200
           from userData).

        Result is cached on ``_cached_available``
        for ``StoreBase`` consumers that don't
        want to re-probe on every check.

        Returns:
            True iff usable.
        """
        if not self._gog_config.is_valid():
            self._cached_available = False
            return False
        available = await self._library.is_available()
        self._cached_available = available
        return available

    async def start_auth(self, **kwargs) -> AuthResult:
        """Start OAuth browser flow — requires the auth-only construction path.

        Pipeline:

        1. Verify ``_auth`` is set (we're in
           auth-mode);
        2. Verify Edge browser is installed
           (GOG's OAuth uses Edge for the device
           flow);
        3. Create the auth shortcut (so Steam UI
           has a launchable entry);
        4. Delegate to
           ``GOGBrowserAuth.start_auth``.

        Args:
            **kwargs: passed-through args (unused
                by GOG but part of the
                ``StoreBase`` signature).

        Returns:
            ``AuthResult``.
        """
        if self._auth is None:
            return AuthResult(
                success=False,
                error="auth_not_configured",
                store="gog",
            )
        if self._edge is None or not self._edge.is_installed:
            logger.info(
                "[GOGStore] Edge not installed — prompting user",
            )
            return AuthResult(
                success=False,
                error="edge_not_installed",
                store="gog",
            )
        await self._ensure_auth_shortcut()
        return cast("AuthResult", await self._auth.start_auth())

    async def complete_auth(self, code: str = "", **kwargs) -> AuthResult:
        """Verify auth completion — just re-probe ``is_available``.

        GOG auth completes asynchronously via
        the browser monitor; this method is the
        UI's "are we done yet?" probe. Returns
        success iff the library is now
        reachable.

        Args:
            code: unused for GOG (other stores
                accept inline codes here).
            **kwargs: unused.

        Returns:
            ``AuthResult``.
        """
        if await self.is_available():
            return AuthResult(success=True, store="gog")
        return AuthResult(
            success=False,
            error="not_authenticated",
            store="gog",
        )

    async def logout(self) -> Result:
        """Clear tokens, remove shortcut, delete the cached auth URL file.

        Two-path: if ``_auth`` is set, delegate
        to ``GOGBrowserAuth.logout`` (which
        clears tokens + emits STORE_LOGOUT);
        otherwise clear tokens + emit STORE_LOGOUT
        directly.

        The ``GOG_AUTH_URL_FILE`` is removed
        too — it's a cache of the last
        authentication URL used by the browser
        flow, and stale entries cause Edge to
        try to resume an expired session.

        Returns:
            ``Result``.
        """
        if self._auth is not None:
            result = await self._auth.logout(
                browser_monitor=self._browser_monitor_from_auth(),
            )
        else:
            await self._tokens.clear()
            await self._bus.emit(
                Events.STORE_LOGOUT,
                store="gog",
            )
            result = Result(success=True)
        auth_url_file = os.path.expanduser(GOG_AUTH_URL_FILE)
        if os.path.isfile(auth_url_file):
            try:
                os.remove(auth_url_file)
            except OSError as e:
                logger.warning(
                    "[GOGStore] could not remove %s: %s",
                    auth_url_file,
                    e,
                )
        return result

    async def get_library(self) -> list[Game] | None:
        """Proxy to ``GOGLibrary.fetch_library``.

        Returns:
            List of games or ``None``.
        """
        return await self._library.fetch_library()

    async def install_game(
        self,
        game_id: str,
        base_path: str | None = None,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        language: str | None = None,
        **kwargs
    ) -> InstallResult:
        """Proxy to ``GOGInstaller.install_game``.

        Args:
            game_id: product id.
            base_path: optional install root.
            progress_cb: optional callback.
            language: optional explicit lang.
            **kwargs: passthrough.

        Returns:
            ``InstallResult``.
        """
        return await self._installer.install_game(
            game_id=game_id,
            base_path=base_path,
            progress_cb=progress_cb,
            language=language,
        )

    async def uninstall_game(self, game_id: str, **kwargs: Any) -> Result:
        """Resolve install path via library + delegate to installer uninstall.

        We do the install-path lookup here
        rather than inside the uninstall
        pipeline so the pipeline stays focused
        on the removal flow itself.

        Args:
            game_id: product id.
            **kwargs: passthrough.

        Returns:
            ``Result``.
        """
        info = self._library.get_installed_game_info(game_id)
        install_path = info.get("install_path") if info else None
        return await self._installer.uninstall_game(game_id=game_id, install_path=install_path)

    async def update_game(
        self,
        game_id: str,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        **kwargs
    ) -> InstallResult:
        """Run gogdl update + adapt the ``Result`` to ``InstallResult``.

        ``GOGUpdatesChecker.update_game``
        returns a ``Result`` (success/error);
        we wrap it as ``InstallResult`` with
        store + game id populated to satisfy the
        ``StoreBase`` signature.

        Args:
            game_id: product id.
            progress_cb: optional (currently
                unused by update flow).
            **kwargs: passthrough.

        Returns:
            ``InstallResult``.
        """
        result = await self._updates.update_game(game_id)
        return InstallResult(
            success=result.success,
            error=result.error,
            store="gog",
            game_id=game_id,
        )

    async def check_for_updates(self) -> list[str]:
        """Proxy to ``GOGUpdatesChecker.check_for_updates``.

        Returns:
            List of game ids with available
            updates.
        """
        return await self._updates.check_for_updates()

    async def get_game_size(self, game_id: str) -> int | None:
        """Probe expected disk size via the installer's planner.

        Used by the UI to show "X GB required"
        before a user confirms an install.
        Always queries the windows build size
        (the larger of the two for cross-
        platform games, so a safer "worst case"
        for free-space checks).

        Args:
            game_id: product id.

        Returns:
            Size in bytes, or ``None`` if
            unknown.
        """
        size = await self._installer._planner.get_expected_disk_size(
            game_id,
            "windows",
        )
        return size if size > 0 else None

    async def get_game_dlcs(self, game_id: str) -> list[dict[str, Any]]:
        """Proxy to ``GOGDlcManager.get_game_dlcs``.

        Args:
            game_id: product id.

        Returns:
            List of DLC dicts.
        """
        return await self._dlc.get_game_dlcs(game_id)

    async def get_available_languages(self, game_id: str) -> list[str]:
        """Proxy to ``GOGDlcManager.get_available_languages``.

        Args:
            game_id: product id.

        Returns:
            Language codes (always at least one).
        """
        return await self._dlc.get_available_languages(game_id)

    async def install_dlc(
        self,
        game_id: str,
        dlc_id: str,
        base_path: str | None = None,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    ) -> Result:
        """Proxy to ``GOGDlcManager.install_dlc``.

        Args:
            game_id: parent game id.
            dlc_id: DLC product id.
            base_path: optional override.
            progress_cb: optional callback.

        Returns:
            ``Result``.
        """
        return await self._dlc.install_dlc(
            game_id=game_id,
            dlc_id=dlc_id,
            base_path=base_path,
            progress_cb=progress_cb,
        )

    async def get_game_store_url(self, game_id: str) -> str | None:
        """Proxy to ``GOGDlcManager.get_game_store_url``.

        Args:
            game_id: product id.

        Returns:
            Storefront URL or ``None``.
        """
        return await self._dlc.get_game_store_url(game_id)

    async def get_game_slug(self, game_id: str) -> str | None:
        """Proxy to ``GOGLibrary.get_game_slug``.

        Args:
            game_id: product id.

        Returns:
            URL slug or ``None``.
        """
        return await self._library.get_game_slug(game_id)

    def get_installed(self) -> list[str]:
        """Proxy to ``GOGLibrary.get_installed``.

        Returns:
            Installed game ids.
        """
        return self._library.get_installed()

    def get_installed_game_info(self, game_id: str) -> dict[str, str | None] | None:
        """Proxy to ``GOGLibrary.get_installed_game_info``.

        Args:
            game_id: product id.

        Returns:
            ``{install_path, executable}`` or
            ``None``.
        """
        return self._library.get_installed_game_info(game_id)

    def migrate_old_markers(self) -> dict[str, int]:
        """Proxy to ``GOGLibrary.migrate_old_markers`` — runs marker upgrade.

        Returns:
            ``{"migrated", "skipped"}`` counts.
        """
        return self._library.migrate_old_markers()

    def _resolve_gogdl_bin(self) -> str:
        """Compute the gogdl binary path from ``plugin_dir/bin/gogdl``.

        ``plugin_dir`` is provided by the host
        (Decky Loader). If unset, we can't
        resolve the binary; return empty string
        (the installer / DLC / updates will
        all return ``gogdl_not_found`` errors
        for any call that needs the binary).

        Missing binary at the resolved path is
        logged at WARN but the path is still
        returned — gogdl might be installed
        later (e.g. binary not yet downloaded
        on first launch).

        Returns:
            Absolute path or empty string.
        """
        if not self._plugin_dir:
            logger.warning(
                "[GOGStore] no plugin_dir; gogdl path unresolvable",
            )
            return ""
        path = os.path.join(
            self._plugin_dir,
            "bin",
            "gogdl",
        )
        if not os.path.isfile(path):
            logger.warning(
                "[GOGStore] gogdl binary not found at %s",
                path,
            )
        else:
            logger.info(
                "[GOGStore] using gogdl at %s",
                path,
            )
        return path

    async def _ensure_auth_shortcut(self) -> None:
        """Create the Steam shortcut that triggers GOG auth via the launcher.

        The dispatcher.py launcher is the entry
        point Steam invokes; it routes to the
        GOG auth flow. We create a Steam
        shortcut pointing at it so the user can
        tap it from Big Picture / Game Mode
        without leaving Steam.

        No-op if shortcut service isn't wired
        up (most likely a unit-test or
        early-boot scenario). Missing
        dispatcher binary logs at WARN — auth
        would still proceed from the API caller
        side, just without a Steam-visible
        entry.
        """
        if self._shortcut_service is None:
            logger.debug(
                "[GOGStore] no shortcut_service; skipping auth shortcut creation",
            )
            return
        launcher = os.path.join(
            self._plugin_dir or "",
            "py_modules",
            "unifideck",
            "launcher",
            "dispatcher.py",
        )
        if not os.path.isfile(launcher):
            logger.warning(
                "[GOGStore] launcher dispatcher not found at %s",
                launcher,
            )
            return
        result = await self._shortcut_service.add_auth_shortcut(
            store="gog",
            launcher_path=launcher,
            title="GOG Sign-In",
        )
        if not result.success:
            logger.warning(
                "[GOGStore] add_auth_shortcut failed: %s",
                result.error,
            )

    def _browser_monitor_from_auth(self) -> OAuthBrowserMonitor | None:
        """Reach into the auth instance to pull out the browser monitor.

        Used by ``logout`` to tear down the
        browser monitor cleanly. The reach-in
        is deliberate — we don't want to make
        the monitor a separate ``__init__``
        parameter just for logout, and the
        coupling is internal to GOG only.

        Returns:
            ``OAuthBrowserMonitor`` or ``None``.
        """
        if self._auth is None:
            return None
        try:
            return self._auth._orch._monitor
        except AttributeError:
            return None
