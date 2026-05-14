"""exe_resolver.py — Find a game's executable when legendary's manifest is silent.

# OP-48g | py_modules/unifideck/stores/epic/exe_resolver.py | Depends: OP-04c

UE titles regularly ship without a launchable ``launch_exe`` in the
legendary manifest. When that happens we scan the install dir for a
plausible game binary, skipping installers / redistributables.
"""
from __future__ import annotations

import glob
import logging
import os
from collections.abc import Callable
from typing import Any

from .legendary import fetch_info

logger = logging.getLogger(__name__)
_SKIP_PATTERNS: tuple[str, ...] = (
    'unins', 'setup', 'install', 'crash', 'ue4prereq', 'redist',
    'vcredist', 'dxsetup', 'directx', 'launcher', 'easyanticheat',
    'battleye', 'eos_', 'eossdk', 'dotnet',
)
_REDIST_PATH_MARKERS: tuple[str, ...] = (
    'redistributables', 'redist', '__installer',
)
_EXE_PATTERNS: tuple[str, ...] = (
    '*.exe',
    'Binaries/Win64/*.exe',
    'Binaries/Win32/*.exe',
    '**/Binaries/Win64/*.exe',
    '**/Binaries/Win32/*.exe',
    '**/Shipping/*.exe',
    'Game/*.exe',
    '**/Game/*.exe',
)


class EpicExeResolver:
    """Resolve the playable executable for an Epic title.

    Tries the legendary manifest's ``launch_exe`` first;
    if that's missing or invalid (common for UE titles),
    scans the install dir with a curated set of glob
    patterns + skip-list for installers/redistributables.
    """

    def __init__(
        self,
        cli_path: str | None,
        find_exe: Callable[[str, list[str] | None], str | None],
        info_timeout_seconds: float,
    ) -> None:
        """Wire the resolver dependencies (config, prefixes, manifests cache).

        Args:
            config: ConfigManager.
            epic_root: Absolute path to the Epic install root.
            install_dir: Absolute path to the game install directory.
            manifest_cache: Pre-loaded manifest cache (avoids
                re-reading from disk on every probe).
        """
        self._cli_path = cli_path
        self._find_exe = find_exe
        self._info_timeout_seconds = info_timeout_seconds

    async def resolve(self, game_id: str) -> dict[str, Any]:
        """Resolve the executable + display title + install path for one game.

        Args:
            game_id: Epic game identifier.

        Returns:
            Dict ``{game_id, install_path, executable, title}``.
            String values are empty on failure to resolve them.
        """
        info = await self._fetch_info(game_id)
        install_path = self._extract_install_path(info)
        title = self._extract_title(info, game_id)
        exe = self._resolve_executable(install_path, info)
        return {
            'game_id': game_id,
            'install_path': install_path or '',
            'executable': exe or '',
            'title': title,
        }

    async def _fetch_info(self, game_id: str) -> dict[str, Any] | None:
        """Wrap ``legendary info`` (returns ``None`` when the CLI is unavailable).

        Args:
            game_id: Epic game identifier.

        Returns:
            Parsed manifest dict, or ``None``.
        """
        if not self._cli_path:
            return None
        return await fetch_info(
            self._cli_path, game_id,
            timeout=self._info_timeout_seconds,
            log_prefix='[epic_exe_resolver]',
        )

    @staticmethod
    def _extract_install_path(info: dict | None) -> str | None:
        """Pull ``install.install_path`` out of a legendary info dict.

        Args:
            info: Parsed legendary info, or ``None``.

        Returns:
            Install path string, or ``None`` if missing.
        """
        if not isinstance(info, dict):
            return None
        install = info.get('install')
        if isinstance(install, dict):
            path = install.get('install_path')
            if isinstance(path, str) and path:
                return path
        return None

    @staticmethod
    def _extract_title(info: dict | None, game_id: str) -> str:
        """Pull ``game.title`` out of a legendary info dict (with fallback).

        Args:
            info: Parsed legendary info, or ``None``.
            game_id: Fallback used when no title is present.

        Returns:
            Display title.
        """
        if isinstance(info, dict):
            game = info.get('game')
            if isinstance(game, dict):
                title = game.get('title')
                if isinstance(title, str) and title:
                    return title
        return game_id

    def _resolve_executable(
        self, install_path: str | None, info: dict | None,
    ) -> str | None:
        """Resolve the executable via manifest hint → glob scan → generic fallback.

        Args:
            install_path: Game install directory.
            info: Parsed legendary info, or ``None``.

        Returns:
            Absolute exe path, or ``None``.
        """
        if not install_path:
            return None
        manifest = info.get('manifest', {}) if isinstance(info, dict) else {}
        manifest_exe = manifest.get('launch_exe') if isinstance(manifest, dict) else None
        if isinstance(manifest_exe, str) and manifest_exe:
            full = os.path.join(install_path, manifest_exe.lstrip('/'))
            if os.path.isfile(full):
                return full
        fallback = self._scan_install_path(install_path)
        if fallback:
            return fallback
        if self._find_exe is not None:
            try:
                return self._find_exe(install_path, None)
            except Exception as e:
                logger.debug('[epic_exe_resolver] find_exe failed: %s', e)
        return None

    @staticmethod
    def _scan_install_path(install_path: str) -> str | None:
        """Glob for executables under the install dir, ignoring installers.

        Tries each pattern in ``_EXE_PATTERNS``, drops any match
        whose basename matches ``_SKIP_PATTERNS`` or whose path
        lives under a redistributables directory, then picks the
        largest survivor.

        Args:
            install_path: Game install directory.

        Returns:
            Path to the largest plausible exe, or ``None``.
        """
        if not install_path or not os.path.isdir(install_path):
            return None
        candidates: list[tuple[int, str]] = []
        for pattern in _EXE_PATTERNS:
            full_pattern = os.path.join(install_path, pattern)
            matches = glob.glob(full_pattern, recursive=('**' in pattern))
            for match in matches:
                basename = os.path.basename(match).lower()
                if any(skip in basename for skip in _SKIP_PATTERNS):
                    continue
                if any(m in match.lower() for m in _REDIST_PATH_MARKERS):
                    continue
                try:
                    size = os.path.getsize(match)
                except OSError:
                    size = 0
                candidates.append((size, match))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]
