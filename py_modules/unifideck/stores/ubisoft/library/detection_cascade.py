"""
Detection cascade — chain of strategies to identify a game on disk.

OP-57g | py_modules/unifideck/stores/ubisoft/library/detection_cascade.py

Identifying an unknown Ubisoft install on disk requires trying several
strategies in order:

1. **manifest match** — read the UPC manifest, look up the space_id;
2. **executable fingerprint** — match the .exe name against known patterns;
3. **directory-name regex** — match the install-dir name against UPC's
   canonical naming convention;
4. **id_map source list** — search the crowd-sourced game-ID DB;
5. **fallback** — register as "Unknown Ubisoft game" with a hash as ID.

``_DetectionCascade`` wires these strategies and returns the first one
that produces a confident match. Each strategy returns a confidence
score; below threshold, the next is tried.
"""

from __future__ import annotations
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from .detection_helpers import (
    load_json_file_safe,
    looks_like_game_install,
    walk_install_candidates,
    write_marker_sync,
)
from .wine_path import wine_path_to_linux

if TYPE_CHECKING:
    from .detection import _InstallDetector
logger = logging.getLogger(__name__)
_INSTALL_MARKER_FILENAME = ".unifideck_ubisoft"


class _DetectionCascade:
    """Chain of strategies to identify an Ubisoft install on disk.

    Each ``detect_via_*`` method implements one strategy; the
    caller tries them in order and stops at the first that
    produces a confident match. Strategies are ordered by
    reliability: marker file → prefix install state → external
    roots → registry InstallDir.
    """

    def __init__(self, parent: _InstallDetector) -> None:
        """Bind the cascade helper to its parent detector.

        Args:
            parent: Owning ``_InstallDetector`` instance.
        """
        self._parent = parent

    def detect_via_marker(
        self,
        space_id: str,
        known_name: str,
        search_roots: list[str],
    ) -> dict[str, Any] | None:
        """Strategy 1: find an install carrying a ``.unifideck_ubisoft`` marker.

        The marker is JSON written at install time and carries the
        canonical ``space_id``, ``install_path``, ``executable``, and
        ``game_title`` — most reliable signal when present.

        Args:
            space_id: UPC space_id we're trying to detect.
            known_name: Last-known display name (used in the result).
            search_roots: Directories to walk.

        Returns:
            Install-info dict on match, ``None`` if no marker found.
        """
        for game_dir, folder in walk_install_candidates(
            search_roots,
        ):
            marker_data = self._load_marker_for_space(
                game_dir,
                space_id,
            )
            if marker_data is None:
                continue
            return self._build_marker_result(
                space_id,
                known_name,
                game_dir,
                folder,
                marker_data,
            )
        return None

    @staticmethod
    def _load_marker_for_space(
        game_dir: str,
        space_id: str,
    ) -> dict | None:
        """Read and validate a marker file for one space_id.

        Args:
            game_dir: Candidate install directory.
            space_id: Expected space_id (the marker must match).

        Returns:
            Marker dict if present and matching, else ``None``.
        """
        marker_path = Path(game_dir) / _INSTALL_MARKER_FILENAME
        if not marker_path.is_file():
            return None
        marker_data = load_json_file_safe(str(marker_path))
        if not isinstance(marker_data, dict):
            return None
        if marker_data.get("space_id") != space_id:
            return None
        return marker_data

    def _build_marker_result(
        self,
        space_id: str,
        known_name: str,
        game_dir: str,
        folder: str,
        marker_data: dict,
    ) -> dict[str, Any]:
        """Build the install-info dict from a matching marker.

        Args:
            space_id: UPC space_id.
            known_name: Last-known display name.
            game_dir: Install directory found.
            folder: Last path component of ``game_dir``.
            marker_data: Parsed marker dict.

        Returns:
            Install-info dict (``space_id``, ``executable``,
            ``install_path``, ``work_dir``, ``title``).
        """
        install_path = marker_data.get("install_path") or game_dir
        executable = self._resolve_marker_executable(
            marker_data,
            install_path,
        )
        return {
            "space_id": space_id,
            "executable": executable,
            "install_path": install_path,
            "work_dir": install_path,
            "title": (marker_data.get("game_title") or known_name or folder),
        }

    def _resolve_marker_executable(
        self,
        marker_data: dict,
        install_path: str,
    ) -> str:
        """Resolve the executable path from the marker (falling back to scan).

        Joins relative paths against ``install_path``; if the recorded
        exe is missing on disk, falls back to ``find_game_executable``
        (scans the install dir for a likely game .exe).

        Args:
            marker_data: Parsed marker dict.
            install_path: Install directory.

        Returns:
            Absolute executable path (empty string if nothing was found).
        """
        executable = marker_data.get("executable", "") or ""
        exe_path = Path(executable) if executable else None
        if exe_path and not exe_path.is_absolute():
            exe_path = Path(install_path) / executable
            executable = str(exe_path)
        if not executable or not Path(executable).exists():
            return self._parent.find_game_executable(install_path) or ""
        return executable

    def detect_via_prefix_install_state(
        self,
        space_id: str,
        prefix_game_roots: list[str],
        normalized_known_name: str,
        known_name: str,
        check_install_state: Callable[[str], bool],
    ) -> dict[str, Any] | None:
        """Strategy 2: find an install via UPC's ``uplay_install.state`` file.

        Within the game's own prefix, UPC writes a state file under
        each installed game. With a known display name, falls back
        to fuzzy-folder-name matching to disambiguate.

        Args:
            space_id: UPC space_id.
            prefix_game_roots: In-prefix candidate directories.
            normalized_known_name: Pre-normalized display name.
            known_name: Raw display name.
            check_install_state: Predicate validating a state file.

        Returns:
            Install-info dict on match, ``None`` otherwise.
        """
        candidates: list[str] = []
        for game_dir, _folder in walk_install_candidates(
            prefix_game_roots,
        ):
            state_file = str(
                Path(game_dir) / "uplay_install.state",
            )
            if check_install_state(state_file):
                candidates.append(game_dir)
        if not candidates:
            return None
        if normalized_known_name:
            for game_dir in candidates:
                if self.fuzzy_folder_match(
                    Path(game_dir).name,
                    normalized_known_name,
                ):
                    return self.build_install_info(
                        space_id,
                        game_dir,
                        known_name or Path(game_dir).name,
                    )
        first_dir = candidates[0]
        return self.build_install_info(
            space_id,
            first_dir,
            known_name or Path(first_dir).name,
        )

    def detect_via_external_roots(
        self,
        space_id: str,
        external_game_roots: list[str],
        normalized_known_name: str,
        known_name: str,
        check_install_state: Callable[[str], bool],
    ) -> dict[str, Any] | None:
        """Strategy 3: same as in-prefix detection but for external install roots.

        External roots include the configured ``default_install_base``
        and ``sdcard_install_base``. Requires a name match because
        external roots may contain unrelated games.

        Args:
            space_id: UPC space_id.
            external_game_roots: External candidate directories.
            normalized_known_name: Pre-normalized display name.
            known_name: Raw display name.
            check_install_state: Predicate validating a state file.

        Returns:
            Install-info dict on match, ``None`` otherwise.
        """
        for game_dir, folder in walk_install_candidates(
            external_game_roots,
        ):
            state_file = str(
                Path(game_dir) / "uplay_install.state",
            )
            if not check_install_state(state_file):
                continue
            if not self.fuzzy_folder_match(
                folder,
                normalized_known_name,
            ):
                continue
            return self.build_install_info(
                space_id,
                game_dir,
                known_name or folder,
            )
        return None

    def detect_via_registry_install_id(
        self,
        space_id: str,
        prefix_path: str,
        known_name: str,
        check_install_state: Callable[[str], bool],
    ) -> dict[str, Any] | None:
        """Strategy 4: read the ``InstallDir`` value out of the Wine registry.

        Looks for the ``HKLM\\Software\\WOW6432Node\\Ubisoft\\Launcher\\Installs\\<id>``
        section that UPC writes after a successful install, decodes
        the Wine path back to Linux, and writes a marker if valid.

        Args:
            space_id: UPC space_id.
            prefix_path: Wine prefix path.
            known_name: Raw display name.
            check_install_state: Predicate validating a state file.

        Returns:
            Install-info dict on match, ``None`` otherwise.
        """
        install_id = self._parent._id_map.resolve_install_id(
            space_id,
        )
        if not install_id:
            return None
        install_section_pattern = self._build_registry_pattern(
            install_id,
        )
        for reg_name in ("pfx/system.reg", "system.reg"):
            reg_path = str(Path(prefix_path) / reg_name)
            result = self._try_registry_file(
                reg_path,
                install_section_pattern,
                space_id,
                install_id,
                prefix_path,
                known_name,
                check_install_state,
            )
            if result is not None:
                return result
        return None

    @staticmethod
    def _build_registry_pattern(install_id: str) -> re.Pattern:
        """Compile the regex that pulls ``InstallDir`` out of system.reg for one id.

        Args:
            install_id: Ubisoft install ID.

        Returns:
            Compiled regex with one capture group for ``InstallDir``.
        """
        return re.compile(
            r"\[Software\\\\(?:Wow6432Node\\\\)?"
            r"Ubisoft\\\\Launcher\\\\Installs\\\\" + re.escape(install_id) + r"\]"
            r'[^\[]*?"InstallDir"\s*=\s*"([^"]*)"',
            re.DOTALL,
        )

    def _try_registry_file(
        self,
        reg_path: str,
        pattern: re.Pattern,
        space_id: str,
        install_id: str,
        prefix_path: str,
        known_name: str,
        check_install_state: Callable[[str], bool],
    ) -> dict[str, Any] | None:
        """Read one ``*.reg`` file and apply the InstallDir pattern.

        On match: converts the Wine path to Linux, validates the
        install state, writes a ``.unifideck_ubisoft`` marker for
        future fast-path detection, and returns the install-info.

        Args:
            reg_path: Path to the registry file.
            pattern: Compiled InstallDir regex.
            space_id: UPC space_id.
            install_id: Ubisoft install ID (for the marker).
            prefix_path: Wine prefix path.
            known_name: Raw display name.
            check_install_state: Predicate validating a state file.

        Returns:
            Install-info dict on match, ``None`` otherwise.
        """
        reg_p = Path(reg_path)
        if not reg_p.is_file():
            return None
        try:
            reg_content = reg_p.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return None
        for match in pattern.finditer(reg_content):
            install_dir_raw = match.group(1).replace("\\\\", "/")
            linux_path = wine_path_to_linux(
                install_dir_raw,
                prefix_path,
            )
            if not self._validate_registry_install(
                linux_path,
                check_install_state,
            ):
                continue
            assert linux_path is not None
            logger.info(
                "[UbisoftLibrary] method 4: registry InstallDir for %s: %s",
                install_id,
                linux_path[:80],
            )
            result = self.build_install_info(
                space_id,
                linux_path,
                known_name or Path(linux_path).name,
            )
            write_marker_sync(
                linux_path,
                space_id,
                result["title"],
            )
            return result
        return None

    @staticmethod
    def _validate_registry_install(
        linux_path: str | None,
        check_install_state: Callable[[str], bool],
    ) -> bool:
        """Validate that a Linux path looks like an installed game.

        Accepts when the path is a directory AND either has
        ``uplay_install.state`` or matches the
        ``looks_like_game_install`` heuristic.

        Args:
            linux_path: Converted Linux path.
            check_install_state: Predicate validating a state file.

        Returns:
            True iff the path is a plausible install.
        """
        if not linux_path or not Path(linux_path).is_dir():
            return False
        state_file = str(
            Path(linux_path) / "uplay_install.state",
        )
        return check_install_state(state_file) or looks_like_game_install(linux_path)

    def build_install_info(
        self,
        space_id: str,
        game_dir: str,
        title_hint: str,
    ) -> dict[str, Any]:
        """Assemble the install-info dict from a detected directory.

        Runs ``find_game_executable`` against the dir to populate
        the ``executable`` field.

        Args:
            space_id: UPC space_id.
            game_dir: Detected install directory.
            title_hint: Best-known display name.

        Returns:
            Install-info dict.
        """
        exe = self._parent.find_game_executable(game_dir) or ""
        title = title_hint or Path(game_dir).name
        return {
            "space_id": space_id,
            "executable": exe,
            "install_path": game_dir,
            "work_dir": game_dir,
            "title": title,
        }

    def fuzzy_folder_match(
        self,
        folder_name: str,
        normalized_known_name: str,
    ) -> bool:
        """Permissive substring match between folder name and normalized title.

        Accepts equality, or either-direction substring containment
        (handles e.g. ``"farcry5"`` vs ``"far cry 5"``).

        Args:
            folder_name: Disk folder name.
            normalized_known_name: Pre-normalized expected display name.

        Returns:
            True iff the names are considered a fuzzy match.
        """
        if not normalized_known_name:
            return False
        normalized_folder = self._parent._id_map.normalize_for_matching(
            folder_name,
        )
        return (
            normalized_folder == normalized_known_name
            or normalized_folder in normalized_known_name
            or normalized_known_name in normalized_folder
        )
