"""
Ubisoft authentication facade — orchestrates the Steam-shortcut auth flow.

OP-58a | py_modules/unifideck/stores/ubisoft/auth/facade.py

Ubisoft Connect has no headless auth flow: the user must sign in
through the UPC GUI. The trick we use is to create a Steam shortcut
that launches UPC inside a dedicated auth prefix; once the user signs
in, UPC writes credentials to the prefix and we propagate them to
every game prefix afterwards (via ``UbisoftSession``, OP-60a).

``UbisoftAuth`` is the orchestration class that wires together the four
sub-modules: ``context`` (UI payload), ``shortcut`` (Steam shortcut
creation), ``session_monitor`` (signal on credential file appearance),
``direct_signin`` (fallback for already-signed-in installs).

``UbisoftAuthState`` and ``UbisoftAuthServices`` are frozen dataclasses
holding the dependencies. State is "owned data" (config, paths,
binaries, callbacks); Services are "external system handles"
(shortcut_service, steamgriddb).
"""

from __future__ import annotations
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from ....core.types import AuthResult, Events, Result
from ....security import (
    audit_auth_flow,
)
from ..binaries import UbisoftBinaryResolver
from ..config import UbisoftConfig
from ..paths import UbisoftPrefixPaths
from ..session import UbisoftSession
from .context import _AuthContext
from .direct_signin import _DirectSignIn
from .session_monitor import _AuthSessionMonitor
from .shortcut import _AuthShortcut
from .shortcut_ops import _ShortcutRegistryOps

if TYPE_CHECKING:
    from ....event_bus.event_bus import EventBus
    from ....services.shortcut import ShortcutService
    from ....steam.steamgriddb import SteamGridDBClient
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UbisoftAuthState:
    """Frozen bundle of dependencies the ``UbisoftAuth`` orchestrator owns.

    Grouped together to keep the ``UbisoftAuth.__init__``
    signature short and to mark these references as immutable
    post-construction.

    Attributes:
        config: Ubisoft configuration snapshot.
        paths: Wine prefix path helpers.
        binaries: UPC binary resolver.
        session: Session payload propagator.
        ensure_auth_prefix: Callable that materialises the
            auth prefix on demand (typically the prefix
            manager's own method).
        queue_auth_assets_ensure: Callable that queues an
            async refresh of the auth-prefix Steam assets
            for one space_id.
    """

    config: UbisoftConfig
    paths: UbisoftPrefixPaths
    binaries: UbisoftBinaryResolver
    session: UbisoftSession
    ensure_auth_prefix: Callable[[], Any]
    queue_auth_assets_ensure: Callable[[str], None]


@dataclass(frozen=True)
class UbisoftAuthServices:
    """External services the auth flow may consume (all optional).

    Attributes:
        plugin_dir: Plugin root directory (resolves relative
            paths like the dispatcher script).
        shortcut_service: Steam shortcut service (None when
            the plugin is invoked headless / standalone).
        steamgriddb: SteamGridDB client for fetching the
            auth-shortcut artwork (None when unavailable).
    """

    plugin_dir: str | None
    shortcut_service: ShortcutService | None
    steamgriddb: SteamGridDBClient | None


class UbisoftAuth:
    """Ubisoft Connect authentication facade — orchestrates the auth flow.

    Coordinates the prefix bootstrap (auth assets), shortcut
    registry operations, direct sign-in, and the post-launch
    session monitor. Exposes the high-level auth/sign-out
    operations the store's RPC layer uses.
    """

    def __init__(
        self,
        bus: EventBus,
        state: UbisoftAuthState,
        services: UbisoftAuthServices,
    ) -> None:
        """Wire dependencies and build the auth sub-orchestrators.

        Args:
            bus: Event bus.
            state: Ubisoft auth state (config, paths, binaries,
                session, and the prefix-bootstrap callbacks).
            services: External services used during auth
                (plugin dir, shortcut service, SteamGridDB).
        """
        self._bus = bus
        self._config = state.config
        self._paths = state.paths
        self._binaries = state.binaries
        self._session = state.session
        self._ensure_auth_prefix = state.ensure_auth_prefix
        self._queue_auth_assets_ensure = state.queue_auth_assets_ensure
        self._plugin_dir = services.plugin_dir
        self._shortcut_service = services.shortcut_service
        self._steamgriddb = services.steamgriddb
        self._registry_ops = _ShortcutRegistryOps(config=self._config)
        self._monitor = _AuthSessionMonitor(
            config=self._config,
            session=self._session,
            queue_auth_assets_ensure=self._queue_auth_assets_ensure,
        )
        self._direct_signin = _DirectSignIn(
            binaries=self._binaries,
            bus=self._bus,
            config=self._config,
            paths=self._paths,
            session=self._session,
            ensure_auth_prefix=self._ensure_auth_prefix,
            queue_auth_assets_ensure=self._queue_auth_assets_ensure,
        )
        self._shortcut = _AuthShortcut(self)
        self._context = _AuthContext(self)

    async def ensure_auth_shortcut(self) -> int | None:
        """Create or update the Ubisoft auth Steam shortcut.

        Idempotent — re-entry on an existing shortcut keeps it.

        Returns:
            True iff the shortcut is present in Steam after this
            call (False on creation failure).
        """
        return await self._shortcut.ensure_auth_shortcut()

    async def auth_shortcut_exists_in_vdf(self) -> bool:
        """Check whether the auth shortcut is registered in ``shortcuts.vdf``.

        Returns:
            True iff Steam's shortcuts file already lists the
            Ubisoft auth entry.
        """
        return await self._shortcut.auth_shortcut_exists_in_vdf()

    async def fetch_auth_shortcut_artwork(
        self,
        unsigned_id: int,
        force: bool = False,
    ) -> None:
        """Trigger a SteamGridDB artwork fetch for the auth shortcut.

        Best-effort — silently skips if SteamGridDB is unavailable.

        Args:
            unsigned_id: Steam shortcut AppID (unsigned form).
            force: Re-download even if artwork already exists.
        """
        await self._context.fetch_auth_shortcut_artwork(
            unsigned_id,
            force=force,
        )

    async def get_auth_shortcut_context(self) -> dict[str, Any]:
        """Return the dict the UI needs to render the auth shortcut state.

        Returns:
            Dict ``{exists, app_id, artwork_status, ...}``.
        """
        return await self._context.get_auth_shortcut_context()

    async def is_available(self) -> bool:
        """Check whether the Ubisoft store can be used on this system.

        Verifies UPC binary availability and any other store-level
        dependencies through the runtime checker.

        Returns:
            True iff every required dependency is present.
        """
        auth_dir = self._config.auth_prefix_dir_expanded
        return self._session.has_valid_credentials(auth_dir)

    @audit_auth_flow(store="ubisoft", method="wine_installer")
    async def start_auth(self) -> AuthResult:
        """Start the Ubisoft Connect auth flow.

        Returns:
            A ``Result`` (success means the auth shortcut has been
            queued and the monitor is watching for credentials).
        """
        return AuthResult(
            success=True,
            store="ubisoft",
            metadata={
                "auth_type": "upc_launch",
                "message": "Sign in through Ubisoft Connect",
            },
        )

    async def complete_auth(
        self,
        code: str = "",
        **kwargs: Any,
    ) -> AuthResult:
        """Confirm auth completed by re-running the availability probe.

        Args:
            code: Unused (legacy parameter for OAuth-style flows).
            **kwargs: Unused.

        Returns:
            ``AuthResult`` — success when ``is_available()`` returns
            True, else ``not_authenticated``.
        """
        if await self.is_available():
            return AuthResult(
                success=True,
                store="ubisoft",
            )
        return AuthResult(
            success=False,
            store="ubisoft",
            error="not_authenticated",
        )

    async def logout(self) -> Result:
        """Logout: clear session file, remove auth prefix, emit STORE_LOGOUT.

        Errors removing the auth prefix are logged but not fatal
        (the session file is the source of truth).

        Returns:
            A successful ``Result``.
        """
        self._session.clear_session_file()
        auth_dir = self._config.auth_prefix_dir_expanded
        if os.path.isdir(auth_dir):
            try:
                shutil.rmtree(auth_dir)
                logger.info(
                    "[UbisoftAuth] removed auth prefix directory",
                )
            except OSError as e:
                logger.error(
                    "[UbisoftAuth] could not remove auth prefix: %s",
                    e,
                )
        await self._bus.emit(
            Events.STORE_LOGOUT,
            store="ubisoft",
        )
        return Result(success=True)

    async def start_auth_session_monitor(self) -> Result:
        """Start the background monitor watching the auth prefix for credentials.

        Returns:
            ``Result`` from the monitor's start sequence.
        """
        return await self._monitor.start()

    def check_auth_session_status(self) -> dict[str, Any]:
        """Return the current auth-session monitor status.

        Returns:
            Dict ``{captured, monitoring}`` from the session monitor.
        """
        return self._monitor.status()

    async def connect_ubisoft_account(self) -> dict[str, Any]:
        """Drive a direct UPC sign-in inside the auth prefix.

        Bypasses the Steam-shortcut detour; intended for
        headless / repair flows.

        Returns:
            Dict reporting the outcome of the sign-in.
        """
        return await self._direct_signin.connect()

    async def _load_registry(self, sm: ShortcutService) -> dict[str, Any]:
        """Load the local shortcuts registry through the shortcut service.

        Returns:
            The registry dict (best-effort — ``{}`` on missing API
            or service error).
        """
        return await self._registry_ops.load(sm)

    async def _register_shortcut(
        self,
        sm: ShortcutService,
        appid: int,
        name: str,
    ) -> None:
        """Persist the auth shortcut entry into the local shortcuts registry.

        Args:
            registry: Current registry dict.
            shortcut: Shortcut entry to add/update.
        """
        await self._registry_ops.register(sm, appid, name)

    async def _clear_compat(
        self,
        sm: ShortcutService,
        appid: int,
    ) -> None:
        """Clear any custom Proton compat-tool selection for the auth shortcut.

        Ensures the auth shortcut uses Steam's default compat
        selection (the Ubisoft auth flow doesn't need a specific
        Proton version).

        Args:
            sm: Shortcut service.
        """
        await self._registry_ops.clear_compat(sm, appid)

    async def _cleanup_legacy_registry(self, sm: ShortcutService) -> None:
        """Remove obsolete entries from the legacy shortcuts registry.

        Best-effort migration helper — drops stale shortcut entries
        left over from earlier plugin versions.

        Args:
            sm: Shortcut service.
        """
        await self._registry_ops.cleanup_legacy(sm)
