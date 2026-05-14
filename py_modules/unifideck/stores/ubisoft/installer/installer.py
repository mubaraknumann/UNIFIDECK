"""
UPC installer orchestration — drives the multi-phase install pipeline.

OP-56a | py_modules/unifideck/stores/ubisoft/installer/installer.py

``UbisoftInstaller`` orchestrates a full UPC install through the
following phases:

1. **bootstrap UPC** — download the UbisoftConnectInstaller.exe from the
   Ubisoft CDN if absent, store in ``installer_cache_dir``;
2. **launch UPC headlessly** — wine-run the installer with the right env;
3. **drive the manual UI** — UPC has no silent-install switch, so the
   manual_ui module operates UPC visually via window-detection;
4. **register the install** — write Unifideck's marker + update the id_map.

Errors at any phase are wrapped into an ``InstallResult`` envelope; the
phase is identified in the error code so the UI can report exactly
which step failed.
"""

from __future__ import annotations
import asyncio
import logging
import os
import subprocess
from collections.abc import Awaitable, Callable
from typing import Any
from ....core.types import InstallResult, Result
from ..binaries import UbisoftBinaryResolver
from ..config import UbisoftConfig
from ..id_map import UbisoftIdMap
from ..library import UbisoftLibrary
from ..paths import UbisoftPrefixPaths
from ..session import UbisoftSession
from . import registry as _reg
from .launch_env import (
    UpcLaunchEnvBuildError,
    _UpcLaunchEnv,
)
from .launcher import _LauncherInstall
from .manual_ui import _ManualUiInstaller
from .registry import _ShortcutRegistry
from .uninstall import _UninstallPipeline
from .update_op import _UpdateOperation

logger = logging.getLogger(__name__)
_UPDATE_TIMEOUT_S = 4 * 60 * 60


class UbisoftInstaller:
    """Orchestrates the full UPC install pipeline for one Ubisoft title.

    Owns the cross-cutting state (active-install PIDs, sub-installers
    for launcher / manual UI / uninstall / update) and exposes the
    store-facing methods: install, uninstall, update,
    open_launcher_for_install, cancel_install_session.
    """

    def __init__(
        self,
        config: UbisoftConfig,
        paths: UbisoftPrefixPaths,
        binaries: UbisoftBinaryResolver,
        id_map: UbisoftIdMap,
        session: UbisoftSession,
        library: UbisoftLibrary,
        bootstrap_game_prefix: Callable[
            [str],
            Awaitable[bool],
        ],
    ) -> None:
        """Wire dependencies and build the manual-UI installer + uninstall pipeline.

        Args:
            config: Ubisoft store config.
            paths: Ubisoft prefix paths.
            binaries: Ubisoft binary resolver.
            id_map: Ubisoft ID map.
            session: Ubisoft session state.
            library: Ubisoft library facade.
            bootstrap_game_prefix: Awaitable callback that
                prepares the per-game Wine prefix before install
                (returns True on success).
        """
        self._config = config
        self._paths = paths
        self._binaries = binaries
        self._id_map = id_map
        self._session = session
        self._library = library
        self._bootstrap_game_prefix = bootstrap_game_prefix
        self._shortcut_registry = _ShortcutRegistry(config)
        self._active_install_pids: dict[str, int] = {}
        self._manual_ui_installer = _ManualUiInstaller(
            config=config,
            library=library,
            id_map=id_map,
            session=session,
            active_install_pids=self._active_install_pids,
        )
        self._uninstall_pipeline = _UninstallPipeline(self)
        self._launcher = _LauncherInstall(self)
        self._update_op = _UpdateOperation(
            id_map=id_map,
            paths=paths,
            session=session,
            build_launch_env=self._build_upc_launch_env,
        )

    async def uninstall_game(
        self,
        game_id: str,
        *,
        delete_prefix: bool = False,
    ) -> Result:
        """Run the uninstall pipeline for one game.

        Args:
            game_id: UPC space_id.
            delete_prefix: When True, the Wine prefix is also wiped
                (otherwise it's kept for credentials/saves).

        Returns:
            A ``Result`` from the uninstall pipeline.
        """
        return await self._uninstall_pipeline.uninstall_game(
            game_id,
            delete_prefix=delete_prefix,
        )

    async def open_launcher_for_install(
        self,
        game_id: str,
    ) -> Result:
        """Open UPC pointed at the install URL for ``game_id``.

        Used when the user wants to install via UPC's GUI directly
        (bypassing our manual-UI driver).

        Args:
            game_id: UPC space_id.

        Returns:
            A ``Result`` capturing the launch outcome.
        """
        return await self._launcher.open_launcher_for_install(
            game_id,
        )

    def _build_upc_launch_env(
        self,
        game_id: str,
        prefix_path: str,
        *,
        prefer_connect_exe: bool = False,
        upc_missing_error: str = "upc_exe_not_found",
    ) -> _UpcLaunchEnv:
        """Build the UPC launch environment (paths, env, umu wrapper).

        Locates UPC inside the prefix (preferring ``UbisoftConnect.exe``
        when ``prefer_connect_exe`` is set, else ``upc.exe``), the
        bundled umu-run wrapper, and a Python interpreter; builds the
        env dict from the binary resolver plus per-game Steam-window
        vars.

        Args:
            game_id: UPC space_id.
            prefix_path: Wine prefix path.
            prefer_connect_exe: Prefer ``UbisoftConnect.exe`` over ``upc.exe``.
            upc_missing_error: Error code to use if UPC isn't found.

        Returns:
            A populated ``_UpcLaunchEnv``.

        Raises:
            UpcLaunchEnvBuildError: UPC, umu-run, or python missing.
        """
        upc_path: str | None = None
        if prefer_connect_exe:
            upc_path = self._paths.find_connect_exe(prefix_path)
        if not upc_path:
            upc_path = self._paths.find_upc_exe(prefix_path)
        if not upc_path:
            raise UpcLaunchEnvBuildError(upc_missing_error)
        umu_run = self._binaries.find_umu_run()
        if not umu_run:
            raise UpcLaunchEnvBuildError("umu_run_not_found")
        python_bin = self._binaries.find_python()
        env = self._binaries.build_umu_env(
            wineprefix=prefix_path,
            gameid=f"umu-ubisoft-{game_id}",
            store_game_id=f"ubisoft:{game_id}",
            steam_window_env=self._build_steam_window_env(
                f"ubisoft:{game_id}",
            ),
        )
        return _UpcLaunchEnv(
            upc_path=upc_path,
            umu_run=umu_run,
            python_bin=python_bin,
            env=env,
        )

    async def install_game(
        self,
        game_id: str,
        *,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        install_path: str | None = None,
    ) -> InstallResult:
        """Run the install pipeline (bootstrap prefix + manual UPC UI install).

        Args:
            game_id: UPC space_id.
            progress_cb: Optional async callback receiving phase progress.
            install_path: Optional override for the install directory.

        Returns:
            An ``InstallResult`` (failure modes: ``prefix_bootstrap_failed``,
            UPC env-build errors, or ``install_exception: <msg>``).
        """
        try:
            logger.info(
                "[UbisoftInstaller] installing game %s",
                game_id,
            )
            if not await self._bootstrap_game_prefix(game_id):
                return InstallResult(
                    success=False,
                    store="ubisoft",
                    game_id=game_id,
                    error="prefix_bootstrap_failed",
                )
            prefix_path = self._paths.get_prefix_path(game_id)
            game_name = self._library._detector._get_game_name(game_id)
            try:
                launch_env = self._build_upc_launch_env(
                    game_id,
                    prefix_path,
                )
            except UpcLaunchEnvBuildError as e:
                return InstallResult(
                    success=False,
                    store="ubisoft",
                    game_id=game_id,
                    error=e.error_code,
                )
            return await self._manual_ui_installer.install_via_upc_ui(
                game_id=game_id,
                game_name=game_name,
                prefix_path=prefix_path,
                upc_path=launch_env.upc_path,
                umu_run=launch_env.umu_run,
                python_bin=launch_env.python_bin,
                env=launch_env.env,
                progress_cb=progress_cb,
                install_path=install_path,
            )
        except Exception as e:
            logger.exception(
                "[UbisoftInstaller] install error for %s: %s",
                game_id,
                e,
            )
            return InstallResult(
                success=False,
                store="ubisoft",
                game_id=game_id,
                error=f"install_exception: {e}",
            )

    def is_install_session_active(self, game_id: str) -> bool:
        """Check whether a UPC install subprocess is still running.

        Uses ``os.kill(pid, 0)`` to probe the PID; cleans up the
        registration if the process has exited.

        Args:
            game_id: UPC space_id.

        Returns:
            True iff a tracked PID for ``game_id`` is still alive.
        """
        pid = self._active_install_pids.get(game_id)
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            self._active_install_pids.pop(game_id, None)
            return False

    async def cancel_install_session(
        self,
        game_id: str,
    ) -> Result:
        """SIGTERM the UPC install subprocess and capture credentials.

        After sending SIGTERM (best-effort), waits 2 s then tries to
        capture and propagate any UPC session credentials that the
        user may have entered before cancelling.

        Args:
            game_id: UPC space_id.

        Returns:
            A successful ``Result`` (cancellation is always reported
            as success — the install just doesn't complete).
        """
        pid = self._active_install_pids.pop(game_id, None)
        if pid is not None:
            try:
                os.kill(pid, 15)
                logger.info(
                    "[UbisoftInstaller] sent SIGTERM to UPC PID %d for %s",
                    pid,
                    game_id,
                )
            except ProcessLookupError:
                logger.info(
                    "[UbisoftInstaller] install process already exited for %s",
                    game_id,
                )
            except OSError as e:
                logger.error(
                    "[UbisoftInstaller] kill failed: %s",
                    e,
                )
        prefix_path = self._paths.get_prefix_path(game_id)
        if prefix_path and os.path.isdir(prefix_path):
            await asyncio.sleep(2)
            captured = self._session.capture(prefix_path)
            if captured:
                self._session.propagate_all_to_all()
                logger.info(
                    "[UbisoftInstaller] post-cancel: propagated session from %s",
                    game_id,
                )
            else:
                logger.info(
                    "[UbisoftInstaller] post-cancel: credentials synced for %s",
                    game_id,
                )
        return Result(success=True)

    async def check_for_updates(self) -> list[str]:
        """Return the list of game IDs that have updates available.

        Currently a no-op stub (always returns ``[]``). UPC's update
        detection is opaque; updates are surfaced lazily via
        ``update_game``.

        Returns:
            Empty list.
        """
        return []

    async def update_game(
        self,
        game_id: str,
    ) -> InstallResult:
        """Apply pending updates for one installed game.

        Delegates to ``_UpdateOperation`` which re-runs UPC in update
        mode against the installed prefix.

        Args:
            game_id: UPC space_id.

        Returns:
            An ``InstallResult``.
        """
        return await self._update_op.update(game_id)

    def inject_install_registry(
        self,
        prefix_path: str,
        install_id: str,
        install_dir: str,
    ) -> None:
        """Write the post-install Ubisoft registry keys into the prefix.

        Args:
            prefix_path: Wine prefix path.
            install_id: Ubisoft install ID.
            install_dir: Game install directory.
        """
        _reg.inject_install_registry(
            prefix_path,
            install_id,
            install_dir,
        )

    def kill_upc_processes(self) -> None:
        """Force-kill every running ``upc.exe`` process (best-effort).

        Useful when UPC hangs after credential capture; runs
        ``pkill -f upc.exe`` with a 5 s timeout.
        """
        try:
            subprocess.run(
                ["pkill", "-f", "upc.exe"],
                capture_output=True,
                timeout=5,
            )
            logger.info(
                "[UbisoftInstaller] killed upc.exe processes",
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning(
                "[UbisoftInstaller] pkill upc.exe failed: %s",
                e,
            )

    def _build_steam_window_env(
        self,
        store_game_id: str | None,
    ) -> dict[str, str]:
        """Build the Steam-window env vars for the UPC subprocess.

        When a shortcut AppID can be resolved, sets ``SteamGameId``,
        ``STEAM_COMPAT_APP_ID``, ``SteamAppId``, and the umu-encoded
        ``UMU_STEAM_GAME_ID``. Otherwise sets every var to ``"0"``
        so umu picks a stable fallback identity.

        Args:
            store_game_id: ``ubisoft:<space_id>`` style identifier,
                or ``None``.

        Returns:
            Env dict.
        """
        appid = self._shortcut_registry.resolve_shortcut_appid(
            store_game_id,
        )
        if appid:
            encoded = str(
                (appid << 32) | 0x02000000,
            )
            logger.info(
                "[UbisoftInstaller] Steam window env: appid=%d store_game_id=%s",
                appid,
                store_game_id or "<none>",
            )
            appid_str = str(appid)
            return {
                "SteamGameId": appid_str,
                "STEAM_COMPAT_APP_ID": appid_str,
                "SteamAppId": appid_str,
                "UMU_STEAM_GAME_ID": encoded,
            }
        logger.info(
            "[UbisoftInstaller] Steam window env: no shortcut appid resolved, using 0",
        )
        return {
            "SteamGameId": "0",
            "STEAM_COMPAT_APP_ID": "0",
            "SteamAppId": "0",
            "UMU_STEAM_GAME_ID": "0",
        }
