"""MicrosoftStore — entry point implementing the Store protocol for the xCloud / Xbox backend."""

from __future__ import annotations
import logging
from pathlib import Path
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
from .microsoft_browser_auth import MicrosoftBrowserAuth
from .microsoft_catalog import MicrosoftCatalogReader
from .microsoft_config import MicrosoftConfig
from .tokens import MicrosoftTokenManager
if TYPE_CHECKING:
    from ...config import ConfigManager
    from ...core.cache_manager import CacheManager
    from ...event_bus.event_bus import EventBus
    from ...services.microsoft_subscription import (
        MicrosoftSubscriptionService,
    )
logger = logging.getLogger(__name__)
class MicrosoftStore(StoreBase):
    """Microsoft / xCloud store backend (``StoreBase`` implementation).

    Bridges the Edge browser (for the OAuth flow), the
    Microsoft token manager (XBL/XSTS chain), the catalog
    reader, and the Game Pass subscription service into a
    cohesive façade. Does not actually install/uninstall —
    Microsoft games run via xCloud streaming through Edge.
    """
    store_info = StoreInfo(
        name="microsoft",
        display_name="Microsoft",
        auth_method="oauth",
        icon_asset="microsoft.png",
        uses_wine=False,
        supports_install=False,
    )

    def __init__(
        self,
        bus: EventBus,
        cache: CacheManager,
        plugin_dir: str | None = None,
        config: ConfigManager | None = None,
        browser_monitor: OAuthBrowserMonitor | None = None,
        shortcut_service: ShortcutService | None = None,
        edge_browser: EdgeBrowser | None = None,
        subscription_service: MicrosoftSubscriptionService | None = None,
    ) -> None:

        """Build the Microsoft store specialists (config, tokens, auth, subscription).

        Reads Microsoft-specific config, builds the token manager
        (with the user's locale plumbed in), then wires the Edge
        browser, subscription service, and remaining specialists.

        Args:
            bus: Event bus.
            cache: Cache manager.
            plugin_dir: Plugin root directory.
            config: ConfigManager.
            browser_monitor: Optional OAuth browser monitor.
            shortcut_service: Optional shortcut service.
            edge_browser: Optional Edge browser wrapper (required
                for xCloud + OAuth flow).
            subscription_service: Optional Game Pass subscription
                probe service.
        """
        super().__init__(bus, cache, plugin_dir, config)
        self._ms_config: MicrosoftConfig = (
            MicrosoftConfig.from_config_manager(config)
        )
        logger.info(
            "[MicrosoftStore] %s",
            self._ms_config.describe(),
        )
        self._config_manager = config
        self._shortcut_service = shortcut_service
        self._edge = edge_browser
        self._subscription_service = subscription_service
        self._tokens = MicrosoftTokenManager(
            config=self._ms_config,
            locale_fn=lambda: get_unifideck_locale(
                self._config_manager,
            ),
            bus=bus,
        )
        self._catalog = MicrosoftCatalogReader(
            config=self._ms_config,
            config_manager=self._config_manager,
        )
        if browser_monitor is not None:
            orchestrator = AuthOrchestrator(
                bus=bus,
                browser_monitor=browser_monitor,
                store_name="microsoft",
            )
            self._auth: MicrosoftBrowserAuth | None = (
                MicrosoftBrowserAuth(
                    bus=bus,
                    orchestrator=orchestrator,
                    tokens=self._tokens,
                    config=self._ms_config,
                    config_manager=self._config_manager,
                )
            )
        else:
            self._auth = None
    async def is_available(self) -> bool:
        """Return True iff the config is valid AND tokens load successfully.

        Token-load failure (missing file, malformed JSON,
        expired refresh token) returns False without raising.

        Returns:
            True iff Microsoft is usable.
        """
        if not self._ms_config.is_valid():
            self._cached_available = False
            return False
        loaded = await self._tokens.load()
        self._cached_available = loaded
        return loaded

    async def start_auth(self, **kwargs) -> AuthResult:

        """Drive the Microsoft OAuth flow through Edge.

        Refuses if no browser monitor was wired (returns
        ``auth_not_configured``) or if Edge is not installed
        (returns ``edge_not_installed`` — the UI shows the
        install prompt). Ensures Edge has controller
        permissions and registers the auth Steam shortcut
        before delegating to the flow.

        Returns:
            ``AuthResult``.
        """
        if self._auth is None:
            return AuthResult(
                success=False,
                error="auth_not_configured",
                store="microsoft",
            )
        if self._edge is None or not self._edge.is_installed:
            logger.info(
                "[MicrosoftStore] Edge not installed — "
                "prompting user to install",
            )
            return AuthResult(
                success=False,
                error="edge_not_installed",
                store="microsoft",
                url=None,
                metadata={"needs_2fa": False},
            )
        EdgeBrowser.ensure_controller_permissions()
        await self._ensure_auth_shortcut()
        return cast("AuthResult", await self._auth.start_auth())
    async def complete_auth(
        self, code: str = "", **kwargs,
    ) -> AuthResult:
        """Probe whether tokens are now valid after an external auth.

        Args:
            code: Reserved for parity; unused (Edge captures
                the code internally).

        Returns:
            ``AuthResult`` reflecting ``is_available``.
        """
        if await self.is_available():
            return AuthResult(success=True, store="microsoft")
        return AuthResult(
            success=False,
            error="not_authenticated",
            store="microsoft",
        )
    async def logout(self) -> Result:
        """Clear tokens, remove the cached auth URL file, and reset Edge state.

        Cancels any pending auth flow, removes
        ``~/.local/share/unifideck/ms_auth_url.txt``, kills
        Edge, clears its cookies, and wipes its profile data.

        Returns:
            ``Result``.
        """
        if self._auth is not None:
            result = await self._auth.logout()
        else:
            await self._tokens.clear()
            await self._bus.emit(
                Events.STORE_LOGOUT, store="microsoft",
            )
            result = Result(success=True)
        auth_url_file = (
            Path("~/.local/share/unifideck/ms_auth_url.txt")
            .expanduser()
        )
        if auth_url_file.is_file():
            try:
                auth_url_file.unlink()
            except OSError as e:
                logger.warning(
                    "[MicrosoftStore] could not remove %s: "
                    "%s",
                    auth_url_file, e,
                )
        if self._edge is not None:
            try:
                self._edge.kill()
                self._edge.clear_cookies()
                EdgeBrowser.clear_profile_data()
            except Exception as e:
                logger.warning(
                    "[MicrosoftStore] Edge cleanup error: "
                    "%s", e,
                )
        return result

    async def get_library(self) -> list[Game] | None:

        """Fetch the xCloud catalog as a list of ``Game`` records.

        Pipeline: verify auth → refresh tokens if stale →
        check the Game Pass subscription gate → build the
        XBL chain → fetch the catalog. Returns an empty list
        (not ``None``) on any failure to avoid masking the
        store from the rest of the UI.

        Returns:
            Catalog ``Game`` list, or empty on any failure.
        """
        if not await self.is_available():
            logger.info(
                "[MicrosoftStore] not authenticated; "
                "returning empty library",
            )
            return []
        fresh = await self._tokens.refresh_if_stale()
        if not fresh:
            logger.error(
                "[MicrosoftStore] token refresh failed; "
                "session is dead",
            )
            await self._tokens.clear()
            return []
        if not await self._check_subscription_gate():
            return []
        chain = await self._tokens.build_chain()
        if chain is None:
            logger.warning(
                "[MicrosoftStore] XBL chain build failed — "
                "proceeding with catalog fetch anyway",
            )
        try:
            return await self._catalog.fetch_games()
        except Exception as e:
            logger.error(
                "[MicrosoftStore] get_library failed: %s", e,
            )
            return []

    async def _check_subscription_gate(self) -> bool:

        """Skip catalog fetch when the user has no active Game Pass tier.

        Emits SYNC_SKIPPED with one of:
          * ``subscription_check_error`` (probe raised)
          * ``no_active_subscription`` (tier=NONE)
          * ``subscription_tier_unknown`` (tier=ACTIVE_UNKNOWN)

        Returns ``True`` (gate passed) when no subscription
        service is wired — legacy behavior so isolation tests
        still work.

        Returns:
            True iff catalog fetch should proceed.
        """
        if self._subscription_service is None:
            logger.debug(
                "[MicrosoftStore] no subscription_service "
                "wired — skipping subscription gate (legacy "
                "behaviour)",
            )
            return True
        from ...core.types import SubscriptionTier
        try:
            tier = await self._subscription_service.get_tier(
                self._tokens,
            )
        except Exception as e:
            logger.warning(
                "[MicrosoftStore] subscription check raised: "
                "%s — skipping sync", e,
            )
            await self._bus.emit(
                Events.SYNC_SKIPPED,
                store="microsoft",
                reason="subscription_check_error",
            )
            return False
        if tier == SubscriptionTier.NONE:
            logger.info(
                "[MicrosoftStore] no active xCloud "
                "subscription — skipping sync",
            )
            await self._bus.emit(
                Events.SYNC_SKIPPED,
                store="microsoft",
                reason="no_active_subscription",
            )
            return False
        if tier == SubscriptionTier.ACTIVE_UNKNOWN:
            logger.warning(
                "[MicrosoftStore] subscription active but "
                "tier unknown — skipping sync pending "
                "capture data",
            )
            await self._bus.emit(
                Events.SYNC_SKIPPED,
                store="microsoft",
                reason="subscription_tier_unknown",
            )
            return False
        logger.info(
            "[MicrosoftStore] active subscription detected "
            "(tier=%s) — fetching catalog",
            tier.value,
        )
        return True
    async def install_game(
        self,
        game_id: str,
        base_path: str | None = None,
        progress_cb: Any = None,
        **kwargs: Any,
    ) -> InstallResult:
        """No-op for Microsoft / xCloud (games stream, they don't install).

        Args:
            game_id: xCloud product ID.
            base_path: Ignored.
            progress_cb: Ignored.

        Returns:
            ``InstallResult`` with ``success=True`` and a null
            install_path.
        """
        return InstallResult(
            success=True,
            store="microsoft",
            game_id=game_id,
            install_path=None,
        )
    async def uninstall_game(
        self, game_id: str, **kwargs: Any,
    ) -> Result:
        """No-op for Microsoft / xCloud.

        Args:
            game_id: xCloud product ID.

        Returns:
            ``Result`` with ``success=True``.
        """
        return Result(success=True)

    async def update_game(
        self,
        game_id: str,
        progress_cb: Any = None,
        **kwargs: Any,
    ) -> InstallResult:

        """No-op for Microsoft / xCloud (catalog updates server-side).

        Args:
            game_id: xCloud product ID.
            progress_cb: Ignored.

        Returns:
            ``InstallResult`` with ``success=True``.
        """
        return InstallResult(
            success=True,
            store="microsoft",
            game_id=game_id,
        )
    async def check_for_updates(self) -> list[str]:
        """Always returns an empty list — xCloud titles update server-side.

        Returns:
            Empty list.
        """
        return []
    async def get_game_size(
        self, game_id: str,
    ) -> int | None:
        """Always ``None`` for streamed Microsoft / xCloud titles.

        Args:
            game_id: xCloud product ID.

        Returns:
            ``None``.
        """
        return None
    async def install_edge(self) -> Result:
        """Install the Edge flatpak via the bundled installer.

        Returns:
            ``Result`` — ``edge_browser_not_configured`` if no
            EdgeBrowser was injected.
        """
        if self._edge is None:
            return Result(
                success=False,
                error="edge_browser_not_configured",
            )
        raw = await self._edge.install()
        return Result(
            success=bool(raw.get("success")),
            error=raw.get("error"),
        )
    def is_edge_installed(self) -> bool:
        """Return True iff Edge is configured and installed.

        Returns:
            True iff the EdgeBrowser is wired and reports installed.
        """
        return (
            self._edge is not None
            and self._edge.is_installed
        )
    async def _ensure_auth_shortcut(self) -> None:
        """Register the Steam auth shortcut that launches our dispatcher.

        Required for Microsoft because the OAuth flow runs in
        Edge as a non-Steam shortcut. Skips silently when no
        shortcut service is wired or the dispatcher file is
        missing.
        """
        if self._shortcut_service is None:
            logger.debug(
                "[MicrosoftStore] no shortcut_service "
                "injected; skipping auth shortcut creation",
            )
            return
        launcher = str(
            Path(self._plugin_dir or "")
            / "py_modules" / "unifideck" / "launcher"
            / "dispatcher.py",
        )
        if not Path(launcher).is_file():
            logger.warning(
                "[MicrosoftStore] launcher dispatcher not "
                "found at %s",
                launcher,
            )
            return
        result = await (
            self._shortcut_service.add_auth_shortcut(
                store="microsoft",
                launcher_path=launcher,
                title="Microsoft Sign-In",
            )
        )
        if not result.success:
            logger.warning(
                "[MicrosoftStore] add_auth_shortcut "
                "failed: %s",
                result.error,
            )