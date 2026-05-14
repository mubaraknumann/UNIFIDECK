"""
UPC manual-UI driver — watches UPC windows and detects install completion.

OP-56e | py_modules/unifideck/stores/ubisoft/installer/manual_ui.py

UPC has no silent-install flag, so the installer must be driven by the
user pressing through the wizard. Once the wizard finishes UPC starts
a service-mode background loop which makes it hard to know when the
install is actually done.

This module exposes ``_ManualInstallDriver`` which:

* snapshots the ``drive_c/Program Files (x86)/.../games/`` directory
  *before* the install (``_snapshot_upc_game_dirs``);
* watches the parent of the install_base for new game-install dirs
  (``_check_new_dirs``);
* uses heuristics from ``looks_like_game_install`` to confirm the
  new directory really is a game install (not a transient temp dir).

Returns the detected install path or ``None`` on timeout.
"""

from __future__ import annotations
import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any
from ....core.types import InstallResult
from ..config import UbisoftConfig
from ..id_map import UbisoftIdMap
from ..library import UbisoftLibrary
from ..library.detection_helpers import looks_like_game_install
from ..session import UbisoftSession
from . import registry as _reg

logger = logging.getLogger(__name__)
_MANUAL_INSTALL_TIMEOUT_S = 2 * 60 * 60
_MANUAL_INSTALL_POLL_INTERVAL_S = 10.0
_STABILITY_WAIT_MAX_POLLS = 360
_STABILITY_POLL_INTERVAL_S = 10.0
_STABILITY_STABLE_THRESHOLD = 3


class _ManualUiInstaller:
    """Drive a UPC manual install — launch UPC, watch the filesystem, detect completion.

    UPC has no silent-install flag, so the wizard must be
    driven by the user. Once the wizard finishes UPC switches
    to a service-mode background loop with no clean termination
    signal — this installer snapshots the install destination
    before launching UPC, polls for new game directories
    (filtered by ``looks_like_game_install``), then waits for
    directory-size stability before declaring the install done.
    """

    def __init__(
        self,
        config: UbisoftConfig,
        library: UbisoftLibrary,
        id_map: UbisoftIdMap,
        session: UbisoftSession,
        active_install_pids: dict[str, int],
    ) -> None:
        """Wire dependencies for the manual-UI install fallback.

        Args:
            config: Ubisoft store config.
            library: Ubisoft library facade.
            id_map: Ubisoft ID map.
            session: Ubisoft session state.
            active_install_pids: Shared dict tracking in-flight
                installer PIDs (keyed by space_id).
        """
        self._config = config
        self._library = library
        self._id_map = id_map
        self._session = session
        self._active_install_pids = active_install_pids

    async def install_via_upc_ui(
        self,
        *,
        game_id: str,
        game_name: str | None,
        prefix_path: str,
        upc_path: str,
        umu_run: str,
        python_bin: str,
        env: dict[str, str],
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None,
        install_path: str | None,
    ) -> InstallResult:
        """Run one manual-install cycle: snapshot → spawn UPC → detect → finalize.

        Args:
            game_id: Ubisoft space_id.
            game_name: Display title (best-effort).
            prefix_path: Wine prefix root.
            upc_path: Path to upc.exe inside the prefix.
            umu_run: Path to the umu-run wrapper.
            python_bin: Python interpreter for umu-run.
            env: Subprocess env (from ``build_umu_env``).
            progress_cb: Optional async callback receiving
                ``{status, message, progress}`` dicts.
            install_path: Optional install-base override; default
                comes from config.

        Returns:
            ``InstallResult`` — success only when a new directory
            was detected AND its size stabilized.
        """
        logger.info(
            "[UbisoftInstaller] install_id unavailable for %s "
            "— launching UPC for manual install",
            game_id,
        )
        self._session.inject_into_prefix(prefix_path)
        install_base, dirs_before, upc_dirs_before = self._snapshot_pre_install(
            install_path, prefix_path
        )
        proc = await self._notify_and_spawn_upc(
            game_id=game_id,
            upc_path=upc_path,
            umu_run=umu_run,
            python_bin=python_bin,
            env=env,
            progress_cb=progress_cb,
        )
        install_dir = await self._poll_for_new_install(
            proc=proc,
            install_base=install_base,
            dirs_before=dirs_before,
            upc_dirs_before=upc_dirs_before,
            progress_cb=progress_cb,
        )
        await self._terminate_upc_gracefully(proc)
        self._active_install_pids.pop(game_id, None)
        self._capture_and_propagate_session(prefix_path)
        if not install_dir:
            return InstallResult(
                success=False,
                store="ubisoft",
                game_id=game_id,
                error="no_install_detected",
            )
        return await self._finalize_manual_install(
            game_id=game_id,
            game_name=game_name,
            install_dir=install_dir,
        )

    async def _notify_and_spawn_upc(
        self,
        *,
        game_id: str,
        upc_path: str,
        umu_run: str,
        python_bin: str,
        env: dict[str, str],
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> asyncio.subprocess.Process:
        """Emit the ``waiting`` progress, spawn UPC, and register the PID.

        Returns:
            Live ``asyncio.subprocess.Process``.
        """
        if progress_cb:
            await progress_cb(
                {
                    "status": "waiting",
                    "message": (
                        "Ubisoft Connect is opening. Please "
                        "install the game from the UPC interface."
                    ),
                    "progress": 0,
                }
            )
        logger.info(
            "[UbisoftInstaller] launching UPC for manual install",
        )
        proc = await asyncio.create_subprocess_exec(
            python_bin,
            umu_run,
            upc_path,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._active_install_pids[game_id] = proc.pid
        return proc

    def _snapshot_pre_install(
        self,
        install_path: str | None,
        prefix_path: str,
    ) -> tuple[str, set[str], dict[str, set[str]]]:
        """Snapshot the install-base and UPC ``games/`` dir contents pre-install.

        Both snapshots feed the polling loop: the install-base
        catches games installed to a user-chosen location, the
        UPC games dir catches games installed to the default
        in-prefix location.

        Args:
            install_path: Install-base override.
            prefix_path: Wine prefix root.

        Returns:
            Tuple ``(install_base, dirs_before, upc_dirs_before)``.
        """
        install_base, dirs_before = self._snapshot_install_base(
            install_path,
        )
        upc_dirs_before = self._snapshot_upc_game_dirs(prefix_path)
        return install_base, dirs_before, upc_dirs_before

    def _capture_and_propagate_session(
        self,
        prefix_path: str,
    ) -> None:
        """Capture the UPC session from the prefix and propagate to other prefixes.

        Lets multi-prefix Ubisoft installs (one prefix per game)
        share the same authentication state without re-prompting.

        Args:
            prefix_path: Prefix that just hosted the install.
        """
        if self._session.capture(prefix_path):
            self._session.propagate_all_to_all()

    def _snapshot_install_base(
        self,
        install_path: str | None,
    ) -> tuple[str, set]:
        """Create the install-base dir if needed and snapshot its current contents.

        Args:
            install_path: Override, or ``None`` (use config default).

        Returns:
            Tuple ``(install_base, dirs_before)``.
        """
        install_base = install_path or self._config.default_install_base_expanded
        os.makedirs(install_base, exist_ok=True)
        dirs_before: set = set()
        try:
            dirs_before = set(os.listdir(install_base))
        except OSError:
            pass
        return install_base, dirs_before

    @staticmethod
    async def _terminate_upc_gracefully(
        proc: asyncio.subprocess.Process,
        timeout: float = 15.0,
    ) -> None:
        """Best-effort SIGTERM with timeout, falling back to SIGKILL.

        Args:
            proc: Live UPC subprocess.
            timeout: Seconds to wait for graceful exit.
        """
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(
                proc.wait(),
                timeout=timeout,
            )
        except (TimeoutError, ProcessLookupError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def _finalize_manual_install(
        self,
        *,
        game_id: str,
        game_name: str | None,
        install_dir: str,
    ) -> InstallResult:
        """Post-install: locate exe, write install marker, measure size, refresh id_map.

        Args:
            game_id: Ubisoft space_id.
            game_name: Display title.
            install_dir: Detected install directory.

        Returns:
            ``InstallResult`` carrying install_path, size_bytes,
            and ``metadata.executable``.
        """
        exe = self._library.find_game_executable(install_dir)
        await self._library.write_install_marker(
            space_id=game_id,
            install_path=install_dir,
            executable=exe or "",
            game_title=game_name or "",
        )
        final_size = _reg.get_directory_size(install_dir)
        logger.info(
            "[UbisoftInstaller] manual install complete: %s (%.0f MB)",
            install_dir,
            final_size / (1024 * 1024),
        )
        try:
            await self._id_map.refresh_from_configurations()
        except Exception as e:
            logger.debug(
                "[UbisoftInstaller] id_map refresh after install failed: %s",
                e,
            )
        return InstallResult(
            success=True,
            store="ubisoft",
            game_id=game_id,
            install_path=install_dir,
            size_bytes=final_size,
            metadata={"executable": exe},
        )

    @staticmethod
    def _snapshot_upc_game_dirs(
        prefix_path: str,
    ) -> dict[str, set]:
        """Snapshot UPC's per-prefix ``games/`` directory contents.

        Checks both root and ``pfx`` layouts so we catch installs
        regardless of which prefix layout is active.

        Args:
            prefix_path: Wine prefix root.

        Returns:
            Dict ``games_dir_path → set of existing entries`` for
            every UPC games dir that exists.
        """
        upc_games_rel = os.path.join(
            "drive_c",
            "Program Files (x86)",
            "Ubisoft",
            "Ubisoft Game Launcher",
            "games",
        )
        candidates = (
            os.path.join(prefix_path, upc_games_rel),
            os.path.join(prefix_path, "pfx", upc_games_rel),
        )
        snapshots: dict[str, set] = {}
        for gdir in candidates:
            if os.path.isdir(gdir):
                try:
                    snapshots[gdir] = set(os.listdir(gdir))
                except OSError:
                    pass
        return snapshots

    async def _poll_for_new_install(
        self,
        *,
        proc: asyncio.subprocess.Process,
        install_base: str,
        dirs_before: set,
        upc_dirs_before: dict[str, set],
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> str | None:
        """Poll the snapshotted directories for a new game install dir.

        Runs every ``_MANUAL_INSTALL_POLL_INTERVAL_S`` seconds up
        to ``_MANUAL_INSTALL_TIMEOUT_S``. Stops if UPC exits before
        a new install is detected. Sends periodic ``waiting`` toasts
        (once per minute) to keep the UI alive. Once a candidate
        appears, hands off to ``_wait_for_install_completion`` for
        the stability check.

        Returns:
            Detected install directory, or ``None`` on timeout /
            UPC exit.
        """
        install_dir: str | None = None
        max_polls = int(
            _MANUAL_INSTALL_TIMEOUT_S / _MANUAL_INSTALL_POLL_INTERVAL_S,
        )
        for iteration in range(max_polls):
            await asyncio.sleep(
                _MANUAL_INSTALL_POLL_INTERVAL_S,
            )
            install_dir = self._check_new_dirs(
                install_base,
                dirs_before,
            )
            if not install_dir:
                for gdir, before in upc_dirs_before.items():
                    found = self._check_new_dirs(gdir, before)
                    if found:
                        install_dir = found
                        break
            if install_dir:
                logger.info(
                    "[UbisoftInstaller] detected install at %s",
                    install_dir,
                )
                await self._notify_install_detected(
                    install_dir,
                    progress_cb,
                )
                await self._wait_for_install_completion(
                    install_dir,
                    progress_cb,
                )
                return install_dir
            if proc.returncode is not None:
                logger.info(
                    "[UbisoftInstaller] UPC exited rc=%d",
                    proc.returncode,
                )
                return None
            if progress_cb and iteration % 6 == 0:
                await progress_cb(
                    {
                        "status": "waiting",
                        "message": (
                            "Waiting for game installation in Ubisoft Connect…"
                        ),
                        "progress": 0,
                    }
                )
        return None

    @staticmethod
    async def _notify_install_detected(
        install_dir: str,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        """Emit a ``installing`` progress event mentioning the detected directory.

        Args:
            install_dir: Detected install directory.
            progress_cb: Optional async callback.
        """
        if not progress_cb:
            return
        await progress_cb(
            {
                "status": "installing",
                "message": (f"Game detected at {os.path.basename(install_dir)}"),
                "progress": 50,
            }
        )

    def _check_new_dirs(
        self,
        base: str,
        before: set,
    ) -> str | None:
        """Compare the current directory listing against the pre-install snapshot.

        Returns the first directory that wasn't in the snapshot
        AND passes ``looks_like_game_install``.

        Args:
            base: Directory to re-list.
            before: Snapshot taken before the install.

        Returns:
            Absolute path of a new game install, or ``None``.
        """
        try:
            now = set(os.listdir(base))
        except OSError:
            return None
        new_dirs = now - before
        for d in new_dirs:
            candidate = os.path.join(base, d)
            if os.path.isdir(candidate) and looks_like_game_install(candidate):
                return candidate
        return None

    async def _wait_for_install_completion(
        self,
        install_dir: str,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        """Wait for the new directory's size to stabilize for several consecutive polls.

        Polls every ``_STABILITY_POLL_INTERVAL_S`` for up to
        ``_STABILITY_WAIT_MAX_POLLS``. Considers the install done
        when the directory size doesn't change for
        ``_STABILITY_STABLE_THRESHOLD`` polls in a row. Emits a
        synthetic 50–90% progress curve while waiting.

        Args:
            install_dir: Detected install directory.
            progress_cb: Optional async callback.
        """
        prev_size = 0
        stable_count = 0
        for _ in range(_STABILITY_WAIT_MAX_POLLS):
            await asyncio.sleep(_STABILITY_POLL_INTERVAL_S)
            curr_size = _reg.get_directory_size(install_dir)
            if curr_size == prev_size and curr_size > 0:
                stable_count += 1
                if stable_count >= _STABILITY_STABLE_THRESHOLD:
                    break
                stable_count = 0
                prev_size = curr_size
                if progress_cb and curr_size > 0:
                    await progress_cb(
                        {
                            "status": "installing",
                            "message": (
                                f"Installing… ({curr_size / (1024**3):.1f} GB)"
                            ),
                            "progress": min(
                                90,
                                50 + stable_count * 10,
                            ),
                        }
                    )
