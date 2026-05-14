"""install.py — Run ``legendary install`` and pipe progress to a callback.

# OP-48d | py_modules/unifideck/stores/epic/install.py | Depends: OP-48a
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
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
from .exe_resolver import EpicExeResolver
from .library import EpicLibraryReader

logger = logging.getLogger(__name__)
_PROGRESS_RE = re.compile(r'(\d+(?:\.\d+)?)\s*%')
ProgressCallback = Callable[[float], Awaitable[None]]


class EpicInstaller:
    """Drive ``legendary install`` / ``legendary uninstall`` and surface progress.

    Pipes the CLI's stdout through ``parse_progress_line``
    (matches ``NN%`` / ``NN.N%`` patterns) and emits
    DOWNLOAD_PROGRESS for each line.
    """

    def __init__(
        self,
        bus: EventBus,
        cli_path: str | None,
        library: EpicLibraryReader,
        exe_resolver: EpicExeResolver,
        default_install_root: str,
        install_timeout_seconds: int = 7200,
        uninstall_timeout_seconds: int = 120,
    ) -> None:
        """Wire dependencies for the Legendary-backed Epic install flow.

        Args:
            bus: Event bus.
            cli_path: Path to the ``legendary`` binary.
            library: Epic library reader.
            exe_resolver: Resolver for the game's primary exe.
            default_install_root: Default install location.
            install_timeout_seconds: Hard timeout for the install
                subprocess (default 2 hours for large titles).
            uninstall_timeout_seconds: Hard timeout for uninstall.
        """
        self._bus = bus
        self._cli_path = cli_path
        self._library = library
        self._exe_resolver = exe_resolver
        self._default_install_root = default_install_root
        self._install_timeout_seconds = install_timeout_seconds
        self._uninstall_timeout_seconds = uninstall_timeout_seconds

    async def install_game(
        self,
        game_id: str,
        base_path: str | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> InstallResult:
        """Install a game via ``legendary install <id> --base-path <root> --with-dlcs --yes``.

        Phases: spawn legendary → drain output (progress + bus) →
        on rc=0, invalidate the installed-games cache and write
        the unifideck manifest via ``_finalize_install``.

        Args:
            game_id: Epic game identifier.
            base_path: Override the configured default install root.
            progress_cb: Optional progress callback receiving 0–100.

        Returns:
            ``InstallResult`` — ``success=False`` on missing CLI
            or non-zero rc.
        """
        if not self._cli_path:
            return InstallResult(
                success=False, store='epic', game_id=game_id,
                error='legendary_not_found',
            )
        base = base_path or os.path.expanduser(self._default_install_root)
        os.makedirs(base, exist_ok=True)
        rc = await self._run_install(game_id, base, progress_cb)
        if rc != 0:
            return InstallResult(
                success=False, store='epic', game_id=game_id,
                error=f'legendary_rc:{rc}',
            )
        self._library.invalidate_installed_cache()
        return await self._finalize_install(game_id, base)

    async def _run_install(
        self,
        game_id: str,
        base: str,
        progress_cb: ProgressCallback | None,
    ) -> int:
        """Spawn the legendary install subprocess and wait for it (timeout-bounded).

        Args:
            game_id: Epic game identifier.
            base: Install base path.
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
            logger.warning('[epic_install] spawn failed: %s', e)
            return -1
        await self._drain_install_output(proc, game_id, progress_cb)
        return await self._wait_with_timeout(proc)

    def _build_install_cmd(self, base: str, game_id: str) -> list[str]:
        """Build the ``legendary install`` argv (always with ``--with-dlcs --yes``).

        Args:
            base: Install base path.
            game_id: Epic game identifier.

        Returns:
            argv list.
        """
        return [
            self._cli_path or 'legendary', 'install', game_id,
            '--base-path', base,
            '--with-dlcs',
            '--yes',
        ]

    async def _drain_install_output(
        self,
        proc: Any,
        game_id: str,
        progress_cb: ProgressCallback | None,
    ) -> None:
        """Pipe legendary stdout through ``_handle_install_line`` line-by-line.

        Args:
            proc: Live subprocess.
            game_id: Epic game identifier.
            progress_cb: Optional progress callback.
        """
        async def _handler(line_str: str) -> None:
            """Per-line handler dispatching Legendary install output to the progress parser."""
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
            log_prefix='[epic_install]',
        )

    async def _handle_install_line(
        self,
        line: str,
        game_id: str,
        progress_cb: ProgressCallback | None,
    ) -> None:
        """Parse a legendary output line for progress and emit DOWNLOAD_PROGRESS.

        Progress parsing only attempted on lines containing
        ``Progress:`` or ``%`` to avoid false positives.

        Args:
            line: Raw stdout line.
            game_id: Epic game identifier.
            progress_cb: Optional callback (errors swallowed).
        """
        if not line:
            return
        if 'Progress:' in line or '%' in line:
            percent = parse_progress_line(line, _PROGRESS_RE)
            if percent is not None and progress_cb is not None:
                try:
                    await progress_cb(percent)
                except Exception as e:
                    logger.debug('[epic_install] progress cb: %s', e)
        await self._bus.emit(
            Events.DOWNLOAD_PROGRESS,
            store='epic', game_id=game_id, line=line,
        )

    async def _finalize_install(
        self, game_id: str, base: str,
    ) -> InstallResult:
        """Post-install: resolve install path / exe / title and write the manifest.

        Args:
            game_id: Epic game identifier.
            base: Install base path (used as fallback for install_path).

        Returns:
            ``InstallResult`` with success=True (manifest-write
            failures are logged but not propagated).
        """
        info = await self._exe_resolver.resolve(game_id)
        install_path = info.get('install_path') or os.path.join(base, game_id)
        executable = info.get('executable') or ''
        title = info.get('title') or game_id
        if install_path and executable:
            try:
                rel = os.path.relpath(executable, install_path)
                await write_manifest(
                    install_path=install_path,
                    store='epic',
                    game_id=game_id,
                    title=title,
                    executable_relative=rel,
                    platform='windows',
                )
            except Exception as e:
                logger.warning('[epic_install] manifest write: %s', e)
        return InstallResult(
            success=True, store='epic', game_id=game_id,
            install_path=install_path,
            executable=executable,
        )

    async def uninstall_game(self, game_id: str) -> Result:
        """Run ``legendary uninstall <id> --yes`` with a timeout.

        Invalidates the installed-games cache on completion
        regardless of rc.

        Args:
            game_id: Epic game identifier.

        Returns:
            ``Result`` — error codes ``legendary_not_found``,
            ``uninstall_timeout``, ``spawn_failed:...``,
            ``legendary_rc:N``; success carries ``data={"game_id": ...}``.
        """
        if not self._cli_path:
            return Result(success=False, error='legendary_not_found')
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli_path, 'uninstall', game_id, '--yes',
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
        self._library.invalidate_installed_cache()
        if proc.returncode != 0:
            return Result(
                success=False, error=f'legendary_rc:{proc.returncode}',
            )
        return Result(success=True, data={'game_id': game_id})
