"""amazon_install.py — Run ``nile install`` and pipe progress to a callback.

# OP-49d | py_modules/unifideck/stores/amazon/amazon_install.py | Depends: OP-49a
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from typing import Any

from ...core.manifest import write_manifest
from ...core.types import Events, InstallResult, Result
from ...event_bus.event_bus import EventBus
from ..shared.cli_install_helpers import (
    drain_install_output,
    parse_progress_line,
    wait_with_timeout,
)
from . import amazon_fuel
from .amazon_library import AmazonLibraryReader

logger = logging.getLogger(__name__)
_PROGRESS_RE = re.compile(r'\[\s*(\d+)\s*%\s*\]')
ProgressCallback = Callable[[float], Awaitable[None]]


class AmazonInstaller:
    """Drive ``nile install`` / ``nile uninstall`` and surface progress.

    Pipes the CLI's stdout through ``parse_progress_line`` and
    emits DOWNLOAD_PROGRESS for each line so the UI gets both
    structured percentage updates and raw log output.
    """

    def __init__(
        self,
        bus: EventBus,
        cli_path: str | None,
        library: AmazonLibraryReader,
        find_exe: Callable[[str, list[str] | None], str | None],
        default_install_root: str,
        install_timeout_seconds: int = 3600,
        uninstall_timeout_seconds: int = 120,
    ) -> None:
        """Wire dependencies for the Nile-backed Amazon install flow.

        Args:
            bus: Event bus.
            cli_path: Path to the ``nile`` binary.
            library: Amazon library reader (resolves install path /
                per-game metadata).
            find_exe: Helper resolving an executable inside a game
                install directory.
            default_install_root: Default install location used
                when no per-game override is set.
            install_timeout_seconds: Hard timeout for the install
                subprocess.
            uninstall_timeout_seconds: Hard timeout for the
                uninstall subprocess.
        """
        self._bus = bus
        self._cli_path = cli_path
        self._library = library
        self._find_exe = find_exe
        self._default_install_root = default_install_root
        self._install_timeout_seconds = install_timeout_seconds
        self._uninstall_timeout_seconds = uninstall_timeout_seconds

    async def install_game(
        self,
        game_id: str,
        base_path: str | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> InstallResult:
        """Install a game via ``nile install <id> --base-path <root>``.

        Phases: spawn nile → drain its output (progress + bus emit) →
        on rc=0, resolve the install path / executable / title and
        write the unifideck manifest.

        Args:
            game_id: Amazon game identifier.
            base_path: Override the configured default install root.
            progress_cb: Optional progress callback receiving 0–100.

        Returns:
            ``InstallResult`` — ``success=False`` on missing nile,
            non-zero rc, or undetected install dir.
        """
        if not self._cli_path:
            return InstallResult(
                success=False, store='amazon', game_id=game_id,
                error='nile_not_found',
            )
        base = base_path or os.path.expanduser(self._default_install_root)
        os.makedirs(base, exist_ok=True)
        rc = await self._run_install(base, game_id, progress_cb)
        if rc != 0:
            return InstallResult(
                success=False, store='amazon', game_id=game_id,
                error=f'nile_rc:{rc}',
            )
        return await self._finalize_install(game_id, base)

    async def _finalize_install(
        self, game_id: str, base: str,
    ) -> InstallResult:
        """Post-install bookkeeping — manifest write and result construction.

        Resolves the actual install dir (from nile's installed.json
        or a default ``<base>/<game_id>`` fallback), locates the
        executable via fuel.json, fetches the title from the
        library, and persists the unifideck manifest.

        Args:
            game_id: Amazon game identifier.
            base: Install base path.

        Returns:
            ``InstallResult`` — ``success=False`` if the install
            dir couldn't be located.
        """
        install_path = await self._resolve_install_path(game_id, base)
        if not install_path:
            return InstallResult(
                success=False, store='amazon', game_id=game_id,
                error='install_dir_not_detected',
            )
        executable = await self._resolve_executable(install_path, game_id)
        title = await self._resolve_title(game_id) or game_id
        if executable:
            try:
                rel = os.path.relpath(executable, install_path)
                await write_manifest(
                    install_path=install_path,
                    store='amazon',
                    game_id=game_id,
                    title=title,
                    executable_relative=rel,
                    platform='windows',
                )
            except Exception as e:
                logger.warning('[amazon_install] manifest write: %s', e)
        return InstallResult(
            success=True, store='amazon', game_id=game_id,
            install_path=install_path,
            executable=executable or '',
        )

    async def _run_install(
        self,
        base: str,
        game_id: str,
        progress_cb: ProgressCallback | None,
    ) -> int:
        """Spawn the nile install subprocess and wait for it (timeout-bounded).

        Args:
            base: Install base path.
            game_id: Amazon game identifier.
            progress_cb: Optional progress callback.

        Returns:
            Subprocess exit code (-1 on spawn failure).
        """
        cmd = self._build_install_cmd(base, game_id)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as e:
            logger.warning('[amazon_install] spawn failed: %s', e)
            return -1
        await self._drain_install_output(proc, game_id, progress_cb)
        return await self._wait_with_timeout(proc)

    def _build_install_cmd(self, base: str, game_id: str) -> list[str]:
        """Build the ``nile install`` argv.

        Args:
            base: Install base path.
            game_id: Amazon game identifier.

        Returns:
            argv list ready for ``asyncio.create_subprocess_exec``.
        """
        return [
            self._cli_path or 'nile', 'install', game_id,
            '--base-path', base,
            '--no-prompt',
        ]

    async def _drain_install_output(
        self,
        proc: Any,
        game_id: str,
        progress_cb: ProgressCallback | None,
    ) -> None:
        """Pipe nile stdout through ``_handle_install_line`` line-by-line.

        Args:
            proc: Live subprocess.
            game_id: Amazon game identifier.
            progress_cb: Optional progress callback.
        """
        async def _handler(line_str: str) -> None:
            """Per-line handler dispatching Nile install output to the progress parser."""
            await self._handle_install_line(line_str, game_id, progress_cb)
        await drain_install_output(proc, _handler)

    async def _wait_with_timeout(self, proc: Any) -> int:
        """Wait for the install subprocess with the configured timeout.

        Args:
            proc: Live subprocess.

        Returns:
            Final exit code, or a synthetic error code on timeout.
        """
        return await wait_with_timeout(
            proc, timeout_seconds=self._install_timeout_seconds,
            log_prefix='[amazon_install]',
        )

    async def _handle_install_line(
        self,
        line: str,
        game_id: str,
        progress_cb: ProgressCallback | None,
    ) -> None:
        """Parse one nile output line for a progress percentage and emit DOWNLOAD_PROGRESS.

        Args:
            line: Raw stdout line.
            game_id: Amazon game identifier.
            progress_cb: Optional callback called with the parsed
                percentage (callback errors are swallowed).
        """
        if not line:
            return
        percent = parse_progress_line(line, _PROGRESS_RE)
        if percent is not None and progress_cb is not None:
            try:
                await progress_cb(percent)
            except Exception as e:
                logger.debug('[amazon_install] progress cb: %s', e)
        await self._bus.emit(
            Events.DOWNLOAD_PROGRESS,
            store='amazon', game_id=game_id, line=line,
        )

    async def _resolve_install_path(
        self, game_id: str, base: str,
    ) -> str | None:
        """Resolve the install directory from nile's state or a sensible fallback.

        First reads ``installed.json``; falls back to ``<base>/<game_id>``
        if no entry exists.

        Args:
            game_id: Amazon game identifier.
            base: Install base path.

        Returns:
            Absolute path string, or ``None`` if neither resolves.
        """
        installed = await self._library.read_installed_ids()
        info = installed.get(game_id)
        if isinstance(info, dict):
            path = info.get('path') or info.get('install_path')
            if isinstance(path, str) and os.path.isdir(path):
                return path
        # Fallback: nile installs under base/<game_id> by default.
        candidate = os.path.join(base, game_id)
        if os.path.isdir(candidate):
            return candidate
        return None

    async def _resolve_executable(
        self, install_path: str | None, game_id: str,
    ) -> str | None:
        """Resolve the playable executable for a game.

        Tries fuel.json first, then the generic ``_find_exe``
        callback if available.

        Args:
            install_path: Game install directory.
            game_id: Amazon game identifier.

        Returns:
            Absolute exe path, or ``None`` on failure.
        """
        if not install_path:
            return None
        exe = amazon_fuel.find_exe_from_fuel(install_path)
        if exe:
            return exe
        if self._find_exe is not None:
            try:
                return self._find_exe(install_path, None)
            except Exception as e:
                logger.debug('[amazon_install] find_exe: %s', e)
        return None

    async def _resolve_title(self, game_id: str) -> str:
        """Look up the game's display title from the owned-games list.

        Args:
            game_id: Amazon game identifier.

        Returns:
            Title string, falling back to ``game_id``.
        """
        owned = await self._library.read_owned_games()
        for game in owned:
            if game.game_id == game_id:
                return game.title
        return game_id

    async def uninstall_game(self, game_id: str) -> Result:
        """Run ``nile uninstall`` and clean the install directory.

        Spawns nile with a timeout. On rc=0, also rmtree's the
        install dir (best-effort) since nile sometimes leaves
        shells behind.

        Args:
            game_id: Amazon game identifier.

        Returns:
            ``Result`` with ``data={"game_id": ...}`` on success;
            error codes ``nile_not_found``, ``uninstall_timeout``,
            ``spawn_failed:...``, ``nile_rc:N`` otherwise.
        """
        if not self._cli_path:
            return Result(success=False, error='nile_not_found')
        installed = await self._library.read_installed_ids()
        info = installed.get(game_id) or {}
        install_path = info.get('path') or info.get('install_path')
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli_path, 'uninstall', game_id, '--no-prompt',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                await asyncio.wait_for(
                    proc.wait(),
                    timeout=self._uninstall_timeout_seconds,
                )
            except TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                return Result(success=False, error='uninstall_timeout')
        except OSError as e:
            return Result(success=False, error=f'spawn_failed:{e}')
        if proc.returncode != 0:
            return Result(
                success=False, error=f'nile_rc:{proc.returncode}',
            )
        if isinstance(install_path, str) and os.path.isdir(install_path):
            try:
                await asyncio.to_thread(
                    shutil.rmtree, install_path, ignore_errors=True,
                )
            except OSError as e:
                logger.debug('[amazon_install] rmtree: %s', e)
        return Result(success=True, data={'game_id': game_id})
