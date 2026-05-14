"""
Detect installed Ubisoft games — find on-disk games not in the registry.

OP-57f | py_modules/unifideck/stores/ubisoft/library/detection.py

When Unifideck is freshly installed or the user has manually copied
game files between prefixes, the install registry may be incomplete:
games may exist on disk that Unifideck doesn't know about.

``_LibraryDetection`` runs a discovery pass over every known install
location (UPC's ``games/`` dir + Unifideck's ``default_install_base``)
and identifies on-disk games that aren't in the registry. The detection
uses heuristics from ``detection_helpers.py`` and ``detection_cascade.py``
to handle the many corner cases (DRM-locked installs, partial installs,
renamed dirs, etc.).
"""

from __future__ import annotations
import logging
from pathlib import Path
from typing import Any
from ..config import UbisoftConfig
from ..id_map import UbisoftIdMap
from .detection_cascade import _DetectionCascade
from .detection_helpers import (
    _DetectionHelpers,
    find_game_executable as _find_game_executable_impl,
    in_prefix_game_roots,
    load_json_file_safe as _load_json_file_safe_impl,
    write_install_marker as _write_install_marker_impl,
)

logger = logging.getLogger(__name__)


class _InstallDetector:
    """Detect installed Ubisoft games on disk and emit install-info dicts.

    Wraps the detection cascade and helpers, exposing a clean API
    for the library facade. Owns the ``_DetectionCascade`` and
    ``_DetectionHelpers`` instances.
    """

    def __init__(
        self,
        config: UbisoftConfig,
        id_map: UbisoftIdMap,
    ) -> None:
        """Build the install-detector with cascade and helper sub-objects.

        The detector probes the filesystem and Wine registry to
        decide whether a game is installed; the cascade and
        helpers split the probe steps into composable pieces.

        Args:
            config: Ubisoft store config.
            id_map: Ubisoft ID map (resolves space_id → install_id).
        """
        self._config = config
        self._id_map = id_map
        self._cascade = _DetectionCascade(self)
        self._helpers = _DetectionHelpers(self)

    @staticmethod
    def find_game_executable(
        install_path: str,
    ) -> str | None:
        """Scan an install directory for the most likely game executable.

        Args:
            install_path: Game install directory.

        Returns:
            Absolute path string to the .exe, or ``None``.
        """
        return _find_game_executable_impl(install_path)

    async def write_install_marker(
        self,
        space_id: str,
        install_path: str,
        executable: str,
        game_title: str = "",
    ) -> None:
        """Write the ``.unifideck_ubisoft`` JSON marker into an install dir.

        Args:
            space_id: UPC space_id.
            install_path: Install directory.
            executable: Relative or absolute exe path.
            game_title: Display name.
        """
        await _write_install_marker_impl(
            space_id,
            install_path,
            executable,
            game_title,
        )

    @staticmethod
    def load_json_file_safe(path: str) -> Any | None:
        """Load a JSON file, returning ``None`` on any error.

        Args:
            path: File path.

        Returns:
            Parsed value or ``None``.
        """
        return _load_json_file_safe_impl(path)

    @staticmethod
    def get_game_official_url(game_id: str) -> str:
        """Return the canonical Ubisoft store URL for one space_id.

        Pure helper — doesn't touch any state.

        Args:
            game_id: Ubisoft space_id.

        Returns:
            Store URL string.
        """
        return f"https://store.ubisoft.com/game?pid={game_id}"

    async def get_installed(self) -> dict[str, Any]:
        """Walk every per-game prefix and emit a map of installed games.

        For each prefix bearing the bootstrap marker, runs
        ``_detect_installed_game`` to identify the game on disk and
        auto-resolves the id_map entry when missing.

        Returns:
            ``{space_id: install_info}`` for every detected install.
        """
        installed: dict[str, Any] = {}
        prefixes_dir = Path(self._config.prefixes_dir_expanded)
        if not prefixes_dir.is_dir():
            return installed
        try:
            entries = list(prefixes_dir.iterdir())
        except OSError as e:
            logger.warning(
                "[UbisoftLibrary] prefixes_dir scan failed: %s",
                e,
            )
            return installed
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if not entry.is_dir():
                continue
            marker_path = entry / self._config.bootstrap_marker
            if not marker_path.is_file():
                continue
            game_info = self._detect_installed_game(
                entry.name,
                str(entry),
            )
            if not game_info:
                continue
            installed[entry.name] = game_info
            await self._auto_resolve_missing_id(
                entry.name,
                str(entry),
                game_info,
            )
        return installed

    def get_installed_game_info(
        self,
        game_id: str,
    ) -> dict[str, Any] | None:
        """Return install info for one game (validates marker first).

        Looks up the game's prefix directory, requires the bootstrap
        marker to be present, runs the detection cascade, and
        auto-resolves any missing id_map entry from the registry.

        Args:
            game_id: UPC space_id.

        Returns:
            Install-info dict (``space_id``, ``executable``,
            ``install_path``, ``work_dir``, ``title``), or ``None``
            if the prefix or marker is absent or detection failed.
        """
        prefix_path = Path(self._config.prefixes_dir_expanded) / game_id
        if not prefix_path.is_dir():
            return None
        marker_path = prefix_path / self._config.bootstrap_marker
        if not marker_path.is_file():
            return None
        info = self._detect_installed_game(
            game_id,
            str(prefix_path),
        )
        if info:
            self._auto_resolve_id_from_registry(
                game_id,
                str(prefix_path),
                info,
            )
        return info

    def _auto_resolve_id_from_registry(
        self,
        space_id: str,
        prefix_path: str,
        game_info: dict[str, Any],
    ) -> None:
        """Synchronous variant — fill missing launch_id from system.reg.

        No-op if the id_map already has a launch_id or
        ubisoftconnect_game_id for this space_id. Otherwise reads
        the install ID from the prefix's system.reg and writes it
        into the id_map.

        Args:
            space_id: UPC space_id.
            prefix_path: Wine prefix path.
            game_info: Detected install info (carries the title).
        """
        existing = self._id_map.get_entry(space_id)
        if existing.get("launch_id") or existing.get("ubisoftconnect_game_id"):
            return
        reg_id = UbisoftIdMap.extract_game_id_from_registry(
            prefix_path,
        )
        if not reg_id:
            return
        self._id_map.merge_entry(
            space_id,
            {
                "install_id": reg_id,
                "launch_id": reg_id,
                "ubisoftconnect_game_id": reg_id,
                "name": game_info.get("title", ""),
            },
        )
        logger.info(
            "[UbisoftLibrary] auto-resolved game ID for %s: %s",
            space_id,
            reg_id,
        )

    async def _auto_resolve_missing_id(
        self,
        space_id: str,
        prefix_path: str,
        game_info: dict[str, Any],
    ) -> None:
        """Async variant — fill missing launch_id from registry or game-ID DB.

        Tries the prefix's system.reg first; on miss, looks up the
        game by display name in the iArtorias game-ID database.

        Args:
            space_id: UPC space_id.
            prefix_path: Wine prefix path.
            game_info: Detected install info (carries the title).
        """
        existing = self._id_map.get_entry(space_id)
        if existing.get("launch_id") or existing.get("ubisoftconnect_game_id"):
            return
        reg_id = UbisoftIdMap.extract_game_id_from_registry(
            prefix_path,
        )
        if not reg_id:
            game_title = game_info.get("title", "")
            if game_title:
                reg_id = await self._id_map.lookup_game_id_by_name(
                    game_title,
                )
        if not reg_id:
            return
        self._id_map.merge_entry(
            space_id,
            {
                "install_id": reg_id,
                "launch_id": reg_id,
                "ubisoftconnect_game_id": reg_id,
                "name": game_info.get("title", ""),
            },
        )
        logger.info(
            "[UbisoftLibrary] auto-resolved game ID for %s: %s",
            space_id,
            reg_id,
        )

    def _detect_installed_game(
        self,
        space_id: str,
        prefix_path: str,
    ) -> dict[str, Any] | None:
        """Run the detection cascade on one prefix to identify the installed game.

        Strategies tried in order: marker → in-prefix install state
        → external roots → registry InstallDir. The first match
        is returned.

        Args:
            space_id: UPC space_id (used to score matches).
            prefix_path: Wine prefix path.

        Returns:
            Install-info dict on success, ``None`` on no match.
        """
        try:
            from ..parser import check_install_state
        except ImportError as e:
            logger.debug(
                "[UbisoftLibrary] ubisoft_parser unavailable: %s",
                e,
            )
            return None
        known_name = self._get_game_name(space_id) or ""
        normalized_known_name = (
            self._id_map.normalize_for_matching(known_name) if known_name else ""
        )
        prefix_game_roots = in_prefix_game_roots(prefix_path)
        external_game_roots = self._helpers.get_external_game_roots()
        method1 = self._cascade.detect_via_marker(
            space_id,
            known_name,
            [*prefix_game_roots, *external_game_roots],
        )
        if method1:
            return method1
        method2 = self._cascade.detect_via_prefix_install_state(
            space_id,
            prefix_game_roots,
            normalized_known_name,
            known_name,
            check_install_state,
        )
        if method2:
            return method2
        if normalized_known_name:
            method3 = self._cascade.detect_via_external_roots(
                space_id,
                external_game_roots,
                normalized_known_name,
                known_name,
                check_install_state,
            )
            if method3:
                return method3
        return self._cascade.detect_via_registry_install_id(
            space_id,
            prefix_path,
            known_name,
            check_install_state,
        )

    def _get_game_name(self, space_id: str) -> str | None:
        """Look up a game's display name from the id_map.

        Args:
            space_id: Ubisoft space_id.

        Returns:
            Display name string, or ``None`` if the id_map has
            no entry for this space_id.
        """
        entry = self._id_map.get_entry(space_id)
        return entry.get("name")
