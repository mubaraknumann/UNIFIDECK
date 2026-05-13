"""``EdgeBrowser`` facade — single entry point for the whole edge_browser package.

OP-15c8 | py_modules/unifideck/auth/edge_browser/edge.py

Combines installer + profile manager + CDP client +
process ops + launch flows into one object. Most
methods are thin pass-throughs to the underlying
components — the facade exists so callers
(``MicrosoftStore.auth``, xCloud launcher) have a
stable, single-import API.

Module-level constants:

* ``PROFILE_DIR`` /  ``LOG_FILE`` — current paths
  under ``~/.local/share/unifideck``;
* ``_LEGACY_*`` — pre-rename paths used by
  ``EdgeProfileManager.migrate_legacy_profile``;
* ``_MS_COOKIE_DOMAINS`` — SQL LIKE patterns for
  logout cookie scrubbing;
* ``_BASE_FLAGS`` — Chrome flags shared by every
  Edge launch.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import launch as _launch, process_ops
from .cdp_client import EdgeCDPClient
from .env import clean_env
from .installer import EdgeInstaller
from .profile import EdgeProfileManager

logger = logging.getLogger(__name__)

PROFILE_DIR = str(
    Path(
        "~/.local/share/unifideck/edge-auth",
    ).expanduser()
)
LOG_FILE = str(
    Path(
        "~/.local/share/unifideck/edge-auth.log",
    ).expanduser()
)
_LEGACY_PROFILE_DIR = str(
    Path(
        "~/.local/share/unifideck/chromium-auth",
    ).expanduser()
)
_LEGACY_LOG_FILE = str(
    Path(
        "~/.local/share/unifideck/chromium-auth.log",
    ).expanduser()
)
_MS_COOKIE_DOMAINS = (
    "%xbox.com%",
    "%microsoft.com%",
    "%live.com%",
    "%microsoftonline.com%",
)
_BASE_FLAGS = [
    "--no-first-run",
    "--disable-translate",
    "--disable-infobars",
    "--disable-session-crashed-bubble",
    "--disable-features=TranslateUI",
    "--password-store=basic",
    "--disable-dev-shm-usage",
    "--disable-background-networking",
]


def _make_profile_manager() -> EdgeProfileManager:
    """Build an ``EdgeProfileManager`` with the module-level paths.

    Factory function (used in places where the
    facade can't pre-create one — e.g. static
    methods that don't have ``self``).

    Returns:
        Fresh ``EdgeProfileManager``.
    """
    return EdgeProfileManager(
        profile_dir=PROFILE_DIR,
        log_file=LOG_FILE,
        legacy_profile_dir=_LEGACY_PROFILE_DIR,
        legacy_log_file=_LEGACY_LOG_FILE,
        cookie_domain_patterns=_MS_COOKIE_DOMAINS,
    )


class EdgeBrowser:
    """One-stop facade combining installer, profile, CDP, launch, kill."""

    def __init__(
        self,
        cdp_port: int = 9222,
        locale_fn: Callable[[], str] | None = None,
    ):
        """Wire up sub-components and run legacy-profile migration.

        Three internal components built in
        constructor (installer, CDP client,
        profile manager). Legacy migration runs
        immediately so subsequent operations see
        the fresh paths.

        Args:
            cdp_port: ``--remote-debugging-port`` for
                auth launches (xCloud uses
                ``port+1``).
            locale_fn: callable returning the active
                locale tag; default returns
                ``"en-US"``.
        """
        self.cdp_port = cdp_port
        self.locale_fn = locale_fn or (lambda: "en-US")
        self.process: subprocess.Popen | None = None
        self._installer = EdgeInstaller(clean_env_fn=clean_env)
        self._cdp = EdgeCDPClient(cdp_port=cdp_port)
        self._profile = _make_profile_manager()
        self._migrate_legacy_profile()

    def _migrate_legacy_profile(self) -> None:
        """Forward to ``EdgeProfileManager.migrate_legacy_profile``.

        Runs once at construction. Idempotent.
        """
        self._profile.migrate_legacy_profile()

    def is_running(self) -> bool:
        """Two-source liveness check — own process + CDP probe.

        We may inherit a running Edge from a prior
        plugin reload (where the process handle was
        lost but the browser is still alive).
        ``_get_browser_ws_url`` catches that case via
        the CDP health endpoint.

        Returns:
            True if Edge is alive.
        """
        if self.process is not None and self.process.poll() is None:
            return True
        return self._get_browser_ws_url() is not None

    def _singleton_paths(self) -> list[str]:
        """Forward to ``EdgeProfileManager._singleton_paths``.

        Returns:
            Three-element list of Singleton* paths.
        """
        return self._profile._singleton_paths()

    def _has_stale_singleton_socket(self) -> bool:
        """Forward to ``EdgeProfileManager._has_stale_singleton_socket``.

        Returns:
            True if cleanup is needed.
        """
        return self._profile._has_stale_singleton_socket()

    def cleanup_stale_profile_state(self) -> None:
        """Forward to ``EdgeProfileManager.cleanup_stale_state``.

        Removes Singleton* artifacts left behind by
        an ungracefully-killed Edge so the next
        launch isn't refused with "Chromium already
        running".
        """
        self._profile.cleanup_stale_state()

    def _get_browser_ws_url(self) -> str | None:
        """Forward to ``EdgeCDPClient.get_browser_ws_url``.

        Returns:
            WS URL or ``None``.
        """
        return self._cdp.get_browser_ws_url()

    def _list_cdp_targets(self) -> list[dict[str, Any]]:
        """Forward to ``EdgeCDPClient.list_targets``.

        Returns:
            List of target dicts.
        """
        return self._cdp.list_targets()

    async def navigate_tab(
        self,
        url: str,
        timeout: float = 15.0,
    ) -> bool:
        """Forward to ``EdgeCDPClient.navigate_tab``.

        Args:
            url: target URL.
            timeout: load timeout.

        Returns:
            True on successful navigation.
        """
        return await self._cdp.navigate_tab(url, timeout=timeout)

    async def _close_all_cdp_targets(
        self,
        *,
        log_prefix: str,
    ) -> bool:
        """Forward to ``EdgeCDPClient.close_all_targets``.

        Args:
            log_prefix: log context tag.

        Returns:
            True if anything was closed.
        """
        return await self._cdp.close_all_targets(log_prefix=log_prefix)

    async def prepare_auth_launch(self) -> None:
        """Close any leftover CDP targets and clean stale singletons.

        Called before each new auth launch to ensure
        we don't reuse a half-dead Edge from a prior
        attempt.
        """
        await self._close_all_cdp_targets(log_prefix="lingering auth")
        self.cleanup_stale_profile_state()

    async def close_auth_browser(self) -> bool:
        """Close all CDP targets + cleanup singletons; report whether anything closed.

        Returns:
            True if any tabs were closed.
        """
        closed = await self._close_all_cdp_targets(log_prefix="auth")
        if closed:
            self.cleanup_stale_profile_state()
        return closed

    @staticmethod
    def ensure_controller_permissions() -> bool:
        """Static wrapper around ``EdgeInstaller.ensure_controller_permissions``.

        Static so callers without a live facade
        (e.g. one-off scripts) can still apply the
        override.

        Returns:
            True on success or already-applied.
        """
        return EdgeInstaller(
            clean_env_fn=clean_env,
        ).ensure_controller_permissions()

    def _flatpak_remote_names(self, scope: str) -> set[str]:
        """Forward to ``EdgeInstaller._flatpak_remote_names``.

        Args:
            scope: ``"--user"`` or ``"--system"``.

        Returns:
            Set of remote names.
        """
        return self._installer._flatpak_remote_names(scope)

    async def _ensure_user_flathub_remote(self) -> bool:
        """Forward to ``EdgeInstaller._ensure_user_flathub_remote``.

        Returns:
            True on success.
        """
        return await self._installer._ensure_user_flathub_remote()

    def find_cmd(self) -> list[str] | None:
        """Forward to ``EdgeInstaller.find_cmd``.

        Returns:
            Argv prefix or ``None``.
        """
        return self._installer.find_cmd()

    @property
    def is_installed(self) -> bool:
        """Forward to ``EdgeInstaller.is_installed``.

        Returns:
            True if Edge is on disk.
        """
        return self._installer.is_installed

    @staticmethod
    def _get_default_browser() -> str | None:
        """Static wrapper around ``EdgeInstaller._get_default_browser``.

        Returns:
            Browser id string or ``None``.
        """
        return EdgeInstaller(clean_env_fn=clean_env)._get_default_browser()

    @staticmethod
    def _restore_default_browser(original: str | None) -> None:
        """Static wrapper around ``EdgeInstaller._restore_default_browser``.

        Args:
            original: previous default browser id.
        """
        EdgeInstaller(
            clean_env_fn=clean_env,
        )._restore_default_browser(original)

    async def install(self) -> dict[str, Any]:
        """Forward to ``EdgeInstaller.install``.

        Returns:
            Typed result dict.
        """
        return await self._installer.install()

    def launch_auth(self, auth_url: str) -> bool:
        """Launch Edge in auth (windowed fullscreen) mode.

        Forwards to ``launch.launch_auth`` with
        ``self`` as the browser arg — the launch
        function reads our ``cdp_port``,
        ``locale_fn``, and stamps ``self.process``
        with the spawned ``Popen``.

        Args:
            auth_url: OAuth start URL.

        Returns:
            True on successful spawn.
        """
        return _launch.launch_auth(self, auth_url)

    def launch_xcloud(self, xcloud_url: str) -> bool:
        """Launch Edge in xCloud kiosk mode.

        Same pattern as ``launch_auth`` but with
        kiosk flags + ``port+1`` for CDP.

        Args:
            xcloud_url: deep-link URL.

        Returns:
            True on successful spawn.
        """
        return _launch.launch_xcloud(self, xcloud_url)

    def kill(self) -> None:
        """Gracefully kill the running Edge, then cleanup state.

        Two-step:

        1. ``process_ops.graceful_kill`` (SIGTERM
           with cookie-flush grace);
        2. ``cleanup_stale_profile_state`` — drop
           any Singleton* left behind.

        Always clears ``self.process`` regardless of
        kill success (so subsequent ``is_running``
        checks don't reference the dead handle).
        """
        process_ops.graceful_kill(self.process)
        self.process = None
        self.cleanup_stale_profile_state()

    @staticmethod
    def has_xbox_session() -> bool:
        """Static check whether the profile has Xbox cookies (skip-login signal).

        Returns:
            True if cookies present.
        """
        return _make_profile_manager().has_xbox_session()

    @staticmethod
    def clear_cookies() -> None:
        """Static wrapper to delete Xbox/Microsoft cookies (logout).

        Used by the "Sign out" UI action. Requires
        Edge to be stopped first; callers ensure that
        via ``kill()`` before invoking. Wipes only
        the four documented cookie domains (xbox.com,
        microsoft.com, live.com, microsoftonline.com)
        — keeps unrelated browsing state intact.
        """
        _make_profile_manager().clear_cookies()

    @staticmethod
    def clear_profile_data() -> None:
        """Static wrapper to delete the entire auth profile directory.

        Stronger than ``clear_cookies`` — drops the
        profile dir + log file entirely. Used by
        "Forget account" UI action and when the
        plugin detects corruption (DB schema
        mismatch after Edge upgrade).
        """
        _make_profile_manager().clear_profile_data()

    async def wait_and_check_crash(self) -> bool:
        """Watch for early Edge crash by polling proc + CDP.

        Forwards to ``process_ops.wait_and_check_crash``
        with our process and CDP probe. On a
        detected crash, clears ``self.process`` so
        subsequent ``is_running`` reflects the
        dead state.

        Returns:
            False on detected crash, True otherwise.
        """
        result = await process_ops.wait_and_check_crash(
            self.process,
            self._cdp.probe_cdp,
            LOG_FILE,
        )
        if not result:
            self.process = None
        return result
