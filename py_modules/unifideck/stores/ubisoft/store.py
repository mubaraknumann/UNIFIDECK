"""
Ubisoft store — Layer-4 implementation of the unified store interface.

OP-55a | py_modules/unifideck/stores/ubisoft/store.py

``UbisoftStore`` is the orchestration class that wires every sub-component
of the Ubisoft sub-package together and exposes them through the
``StoreBase`` contract used by the rest of the plugin (RPC mixins,
service layer, registry). It owns one instance each of:

* ``UbisoftConfig`` (OP-55b) — frozen configuration snapshot.
* ``UbisoftPrefixPaths`` (OP-55c) — Wine prefix path enumeration helpers.
* ``UbisoftBinaryResolver`` (OP-55d) — UPC binary discovery.
* ``UbisoftAuth`` (OP-58a) — auth flow via Steam shortcut.
* ``UbisoftLibrary`` (OP-57a) — game library facade.
* ``UbisoftInstaller`` (OP-56a) — installer pipeline.
* ``UbisoftPrefixManager`` (OP-59a) — Wine prefix lifecycle.
* ``UbisoftSession`` (OP-60a) — UPC session payload propagation.

The ``_shortcut_service`` attribute is left at ``None`` at construction
time and injected post-discovery by ``services/bootstrap/store_injector.py``;
see the ``_STORE_INJECTIONS`` table for the wiring entry.

Implements the standard ``StoreBase`` API: ``store_info``, ``is_authed``,
``auth``, ``logout``, ``library``, ``install``, ``uninstall``, ``launch``,
etc. — every method is delegated to the appropriate sub-component.
"""

from __future__ import annotations
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast
from ...core.types import (
    AuthResult,
    Game,
    InstallResult,
    Result,
    StoreInfo,
)
from ..shared.store_base import StoreBase
from .specialists import build_ubisoft_specialists

if TYPE_CHECKING:
    from ...config import ConfigManager
    from ...core.cache_manager import CacheManager
    from ...event_bus.event_bus import EventBus
    from ...services.shortcut import ShortcutService
    from ...steam.steamgriddb import SteamGridDBClient
    from .auth import UbisoftAuth
    from .installer import UbisoftInstaller
    from .library import UbisoftLibrary
logger = logging.getLogger(__name__)


class UbisoftStore(StoreBase):
    """Ubisoft Connect store implementation.

    Glues together the Ubisoft library, installer, auth, and
    session specialists behind the ``StoreBase`` interface
    exposed to the rest of Unifideck.
    """

    store_info = StoreInfo(
        name="ubisoft",
        display_name="Ubisoft",
        auth_method="shortcut",
        icon_asset="ubisoft.png",
        uses_wine=True,
        supports_install=True,
    )

    def __init__(
        self,
        bus: EventBus,
        cache: CacheManager,
        plugin_dir: str | None = None,
        config: ConfigManager | None = None,
        shortcut_service: ShortcutService | None = None,
        steamgriddb: SteamGridDBClient | None = None,
    ) -> None:
        """Build the Ubisoft specialists and bind them to the store.

        Delegates the construction of the library / installer /
        auth / session subgraphs to ``build_ubisoft_specialists``,
        then stores the references.

        Args:
            bus: Event bus.
            cache: Cache manager.
            plugin_dir: Plugin root directory.
            config: ConfigManager.
            shortcut_service: Optional shortcut service
                (passed to the auth orchestrator).
            steamgriddb: Optional SteamGridDB client for grid art.
        """
        super().__init__(bus, cache, plugin_dir, config)
        specialists = build_ubisoft_specialists(
            bus=bus,
            config_mgr=config,
            plugin_dir=plugin_dir,
            shortcut_service=shortcut_service,
            steamgriddb=steamgriddb,
        )
        self._config = specialists.config
        self._paths = specialists.paths
        self._binaries = specialists.binaries
        self._id_map = specialists.id_map
        self._session = specialists.session
        self._installer_cache = specialists.installer_cache
        self._prefix_mgr = specialists.prefix_mgr
        self._library: UbisoftLibrary = specialists.library
        self._installer: UbisoftInstaller = specialists.installer
        self._auth: UbisoftAuth = specialists.auth
        self._ubi_config = specialists.config

    async def is_available(self) -> bool:
        """Check whether the Ubisoft store is available on this system.

        Delegates to the auth specialist and caches the result on
        the store instance for cheap re-queries.

        Returns:
            True iff the store is ready to authenticate / serve
            a library (e.g. dependencies installed).
        """
        available = await self._auth.is_available()
        self._cached_available = available
        return available

    async def start_auth(self, **kwargs: Any) -> AuthResult:
        """Start the Ubisoft Connect authentication flow.

        Ensures the auth shortcut exists in Steam, kicks off the
        post-launch session monitor, then delegates the actual
        auth start to the auth specialist.

        Args:
            **kwargs: Forwarded to the auth specialist (currently unused).

        Returns:
            An ``AuthResult`` describing the initial auth state.
        """
        await self._auth.ensure_auth_shortcut()
        await self._auth.start_auth_session_monitor()
        return cast("AuthResult", await self._auth.start_auth())

    async def complete_auth(
        self,
        code: str = "",
        **kwargs: Any,
    ) -> AuthResult:
        """Complete a pending Ubisoft authentication.

        Args:
            code: Optional auth payload (kept for store-base parity;
                unused by Ubisoft which uses shortcut-based auth).
            **kwargs: Forwarded to the auth specialist.

        Returns:
            An ``AuthResult`` describing the final auth state.
        """
        return await self._auth.complete_auth(code, **kwargs)

    async def logout(self) -> Result:
        """Log the user out of Ubisoft Connect.

        Delegates to the auth specialist which clears the
        persisted credentials and emits the relevant events.

        Returns:
            A ``Result`` summarising the logout outcome.
        """
        return await self._auth.logout()

    async def get_library(self) -> list[Game] | None:
        """Return the user's full Ubisoft Connect library.

        Returns:
            List of ``Game`` records, or ``None`` if the library
            couldn't be fetched (e.g. unauthenticated).
        """
        return await self._library.get_library()

    async def install_game(
        self,
        game_id: str,
        *,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        install_path: str | None = None,
        **kwargs: Any,
    ) -> InstallResult:
        """Install a Ubisoft game via the installer pipeline.

        Args:
            game_id: Ubisoft space_id.
            progress_cb: Optional async callback receiving install
                progress events (passed through to the installer).
            install_path: Override the default install location.
            **kwargs: Accepted for store-base parity; ignored.

        Returns:
            An ``InstallResult``.
        """
        return await self._installer.install_game(
            game_id,
            progress_cb=progress_cb,
            install_path=install_path,
        )

    async def uninstall_game(
        self,
        game_id: str,
        *,
        delete_prefix: bool = False,
        **kwargs: Any,
    ) -> Result:
        """Uninstall a Ubisoft game.

        Args:
            game_id: Ubisoft space_id.
            delete_prefix: When True, also delete the whole Wine
                prefix used by this game.
            **kwargs: Accepted for store-base parity; ignored.

        Returns:
            A ``Result``.
        """
        return await self._installer.uninstall_game(
            game_id,
            delete_prefix=delete_prefix,
        )

    async def update_game(
        self,
        game_id: str,
        **kwargs: Any,
    ) -> InstallResult:
        """Update one installed Ubisoft game.

        Args:
            game_id: Ubisoft space_id.
            **kwargs: Accepted for store-base parity; ignored.

        Returns:
            An ``InstallResult``.
        """
        return await self._installer.update_game(game_id)

    async def check_for_updates(self) -> list[str]:
        """Check every installed Ubisoft game for available updates.

        Returns:
            List of space_ids whose installed build is older than
            the latest available revision.
        """
        return await self._installer.check_for_updates()

    async def get_game_size(
        self,
        game_id: str,
    ) -> int | None:
        """Return the on-disk size of a Ubisoft game install.

        Args:
            game_id: Ubisoft space_id.

        Returns:
            Always ``None`` — Ubisoft doesn't expose a size hint
            distinct from the generic prefix-walker fallback.
        """
        return None

    async def get_installed(self) -> dict[str, Any]:
        """Return the dict of installed Ubisoft games.

        Returns:
            ``{space_id: install_info}`` for every detected install.
        """
        return await self._library.get_installed()

    def get_installed_game_info(
        self,
        game_id: str,
    ) -> dict[str, Any] | None:
        """Return install info for one specific Ubisoft game.

        Args:
            game_id: Ubisoft space_id.

        Returns:
            Install info dict (install path, executable, …), or
            ``None`` if the game isn't installed.
        """
        return self._library.get_installed_game_info(game_id)

    async def write_install_marker(
        self,
        space_id: str,
        install_path: str,
        executable: str,
        game_title: str = "",
    ) -> None:
        """Persist an install marker so the game is visible without rescan.

        Used after a successful install to record the on-disk state
        (install_path, executable) and the title for SteamGridDB
        lookups; the marker is consumed by subsequent ``get_installed``
        calls.

        Args:
            space_id: Ubisoft space_id.
            install_path: Absolute path inside the prefix (Windows-style).
            executable: Game executable name (relative to install_path).
            game_title: Display title (used for artwork resolution).
        """
        await self._library.write_install_marker(
            space_id=space_id,
            install_path=install_path,
            executable=executable,
            game_title=game_title,
        )

    def find_game_executable(
        self,
        install_path: str,
    ) -> str | None:
        """Locate the game executable inside an install directory.

        Args:
            install_path: Absolute path to the install directory.

        Returns:
            Path to the most likely executable, or ``None`` if no
            candidate could be identified.
        """
        return self._library.find_game_executable(install_path)

    def is_install_session_active(self, game_id: str) -> bool:
        """Check whether an install session is currently in flight for a game.

        Args:
            game_id: Ubisoft space_id.

        Returns:
            True iff the installer is currently processing this game.
        """
        return self._installer.is_install_session_active(game_id)

    async def cancel_install_session(
        self,
        game_id: str,
    ) -> Result:
        """Cancel an in-flight install session for one game.

        Args:
            game_id: Ubisoft space_id.

        Returns:
            A ``Result`` — succeeds even if no session was active.
        """
        return await self._installer.cancel_install_session(
            game_id,
        )

    async def open_launcher_for_install(
        self,
        game_id: str,
    ) -> Result:
        """Open UPC against the install URL of one game.

        Used when the user wants UPC to handle the install itself
        (rather than driving it through the Unifideck installer).

        Args:
            game_id: Ubisoft space_id.

        Returns:
            A ``Result`` once UPC has been spawned.
        """
        return await self._installer.open_launcher_for_install(
            game_id,
        )

    def resolve_install_id(
        self,
        space_id: str,
    ) -> str | None:
        """Resolve the Ubisoft install_id for one space_id.

        Args:
            space_id: Ubisoft space_id.

        Returns:
            Install ID string, or ``None`` if unknown.
        """
        return self._id_map.resolve_install_id(space_id)

    def resolve_launch_id(
        self,
        space_id: str,
    ) -> str | None:
        """Resolve the Ubisoft launch_id for one space_id.

        Args:
            space_id: Ubisoft space_id.

        Returns:
            Launch ID string, or ``None`` if unknown.
        """
        return self._id_map.resolve_launch_id(space_id)

    async def get_auth_shortcut_context(
        self,
    ) -> dict[str, Any]:
        """Return the context needed to render the auth shortcut in the UI.

        Returns:
            Dict with the AppID, display name, artwork status, and
            any other fields the auth panel needs.
        """
        return await self._auth.get_auth_shortcut_context()

    async def start_auth_session_monitor(self) -> Result:
        """Start the background monitor that captures UPC credentials post-auth.

        Idempotent — re-entry while monitoring returns a success
        Result without spawning a second monitor.

        Returns:
            A ``Result``.
        """
        return await self._auth.start_auth_session_monitor()

    def check_auth_session_status(self) -> dict[str, Any]:
        """Return the current auth-session monitor status.

        Returns:
            Dict ``{captured, monitoring}`` — whether credentials
            have been captured and whether the monitor is alive.
        """
        return self._auth.check_auth_session_status()

    async def connect_ubisoft_account(
        self,
    ) -> dict[str, Any]:
        """Trigger a direct UPC sign-in (without the Steam-shortcut detour).

        Returns:
            Dict reporting the outcome of the direct sign-in.
        """
        return await self._auth.connect_ubisoft_account()

    def sync_ubisoft_credentials(self) -> dict[str, Any]:
        """Retroactively propagate captured credentials to every game prefix.

        Used after a successful auth to ensure existing per-game
        prefixes have the current credentials before the next launch.

        Returns:
            Dict summarising how many prefixes were updated.
        """
        return self._session.retroactive_sync()

    async def repair_prefix(self, space_id: str) -> Result:
        """Restore the prefix for one game (reset state then reinject hooks).

        Drives the prefix manager's repair routine, then re-injects the
        UPC session payload and the install registry keys so launches
        see a clean environment with current credentials.

        Args:
            space_id: UPC space_id of the game.

        Returns:
            A ``Result`` (failure mode: ``"prefix_repair_failed"``).
        """
        success = await self._prefix_mgr.repair_prefix(space_id)
        if not success:
            return Result(
                success=False,
                error="prefix_repair_failed",
            )
        prefix_path = self._paths.get_prefix_path(space_id)
        self._session.inject_into_prefix(prefix_path)
        install_id = self._id_map.resolve_install_id(space_id)
        if install_id:
            game_info = self._library._detector._detect_installed_game(
                space_id,
                prefix_path,
            )
            if game_info and game_info.get("install_path"):
                self._installer.inject_install_registry(
                    prefix_path,
                    install_id,
                    game_info["install_path"],
                )
        return Result(success=True)

    def get_game_official_url(
        self,
        game_id: str,
    ) -> str | None:
        """Return the canonical Ubisoft store URL for one game.

        Args:
            game_id: Ubisoft space_id.

        Returns:
            Store URL string, or ``None`` if unknown.
        """
        return self._library.get_game_official_url(game_id)

    def kill_upc_processes(self) -> None:
        """Forcefully terminate any running UPC processes.

        Used after install/uninstall to make sure UPC doesn't
        hold file handles open in the prefix.
        """
        self._installer.kill_upc_processes()
