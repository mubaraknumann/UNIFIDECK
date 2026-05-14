"""
Detection helpers — pure functions used by the detection cascade.

OP-57h | py_modules/unifideck/stores/ubisoft/library/detection_helpers.py

A grab-bag of pure-function helpers shared by ``detection.py`` and
``detection_cascade.py``:

* ``looks_like_game_install(path)`` — heuristic checks (size, exe present);
* ``extract_space_id_from_manifest(path)`` — parse a manifest and pull
  the space_id;
* ``fingerprint_executable(exe_name)`` — normalise an .exe name for
  matching against the known-fingerprint dict;
* ``normalise_install_dir_name(name)`` — strip trademark glyphs +
  version suffixes for fuzzy matching.

All helpers are stateless and safe to call concurrently.
"""

from __future__ import annotations
import datetime
import glob
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .detection import _InstallDetector
logger = logging.getLogger(__name__)
_EXE_SKIP_PATTERNS = (
    "unins",
    "setup",
    "install",
    "crash",
    "redist",
    "vcredist",
    "dxsetup",
    "dotnet",
    "upc",
    "uplay",
)
_GAME_INSTALL_MIN_SIZE = 100 * 1024 * 1024
_IN_PREFIX_GAMES_PATH = str(
    Path("drive_c")
    / "Program Files (x86)"
    / "Ubisoft"
    / "Ubisoft Game Launcher"
    / "games"
)
_INSTALL_MARKER_FILENAME = ".unifideck_ubisoft"


def load_json_file_safe(path: str) -> Any | None:
    """Load a JSON file with permissive error handling.

    Args:
        path: Absolute file path.

    Returns:
        Parsed JSON, or ``None`` on read/parse failure.
    """
    try:
        return json.loads(
            Path(path).read_text(
                encoding="utf-8",
                errors="replace",
            ),
        )
    except (OSError, json.JSONDecodeError):
        return None


def walk_install_candidates(
    roots: list[str],
) -> Iterator[tuple[str, str]]:
    """Yield ``(absolute_path, name)`` for every first-level subdir under each root.

    Roots that don't exist are skipped silently. Within each
    root, non-directory entries are filtered out.

    Args:
        roots: Candidate base directories.

    Yields:
        Tuple ``(absolute_path, leaf_name)`` for each candidate.
    """
    for base_dir in roots:
        base = Path(base_dir)
        if not base.is_dir():
            continue
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            yield str(entry), entry.name


def in_prefix_game_roots(prefix_path: str) -> list[str]:
    """Compute the two possible in-prefix UPC games-dir paths.

    Args:
        prefix_path: Wine prefix root.

    Returns:
        List ``[<root>/drive_c/.../games, <root>/pfx/drive_c/.../games]``.
    """
    prefix = Path(prefix_path)
    return [
        str(prefix / _IN_PREFIX_GAMES_PATH),
        str(prefix / "pfx" / _IN_PREFIX_GAMES_PATH),
    ]


def find_game_executable(
    install_path: str,
) -> str | None:
    """Scan an install directory for the most plausible game executable.

    Globs ``*.exe`` and ``**/*.exe`` (recursive), drops names
    matching installer / launcher / redistributable patterns,
    and picks the largest survivor.

    Args:
        install_path: Game install directory.

    Returns:
        Absolute exe path, or ``None`` if nothing plausible.
    """
    if not install_path or not Path(install_path).is_dir():
        return None
    candidates: list[tuple[str, int]] = []
    for pattern in ("*.exe", "**/*.exe"):
        for exe_path in glob.glob(
            str(Path(install_path) / pattern),
            recursive=True,
        ):
            basename = Path(exe_path).name.lower()
            if any(skip in basename for skip in _EXE_SKIP_PATTERNS):
                continue
            try:
                size = Path(exe_path).stat().st_size
                candidates.append((exe_path, size))
            except OSError:
                continue
    if not candidates:
        logger.warning(
            "[UbisoftLibrary] no executable found in %s",
            install_path,
        )
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    result, size = candidates[0]
    logger.info(
        "[UbisoftLibrary] found executable (%.1f MB): %s",
        size / (1024 * 1024),
        result,
    )
    return result


def looks_like_game_install(path: str) -> bool:
    """Heuristic check for whether a directory is a real game install.

    True if (a) any .exe is found within 2 levels OR (b) the
    directory's total size exceeds 100 MiB. Used to filter out
    transient temp/setup dirs during install detection.

    Args:
        path: Candidate directory.

    Returns:
        True iff the directory looks like a real install.
    """
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                if f.lower().endswith(".exe"):
                    return True
            depth = root[len(path) :].count(os.sep)
            if depth >= 2:
                break
        total = 0
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += (Path(root) / f).stat().st_size
                except OSError:
                    continue
                if total > _GAME_INSTALL_MIN_SIZE:
                    return True
    except OSError:
        pass
    return False


async def write_install_marker(
    space_id: str,
    install_path: str,
    executable: str,
    game_title: str = "",
) -> None:
    """Atomically write the ``.unifideck_ubisoft`` marker into an install directory.

    The marker is what subsequent library scans use to
    associate a directory with its Ubisoft space_id.

    Args:
        space_id: Ubisoft space_id.
        install_path: Game install directory.
        executable: Resolved absolute exe path.
        game_title: Display title (best-effort).
    """
    try:
        marker_data = {
            "space_id": space_id,
            "game_title": game_title,
            "install_path": install_path,
            "executable": executable,
            "install_date": (datetime.datetime.now().isoformat()),
        }
        install_p = Path(install_path)
        marker_path = install_p / _INSTALL_MARKER_FILENAME
        tmp_path = marker_path.with_suffix(
            marker_path.suffix + ".tmp",
        )
        install_p.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(
            json.dumps(marker_data, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(marker_path)
        logger.info(
            "[UbisoftLibrary] wrote install marker for %s",
            space_id,
        )
    except OSError as e:
        logger.warning(
            "[UbisoftLibrary] marker write failed: %s",
            e,
        )


def write_marker_sync(
    install_path: str,
    space_id: str,
    title: str,
) -> None:
    """Synchronous fallback marker write (no atomic guarantee).

    Used by code paths that can't await — e.g. process-level
    install hooks. No-op when the marker already exists.

    Args:
        install_path: Game install directory.
        space_id: Ubisoft space_id.
        title: Display title.
    """
    marker_path = Path(install_path) / _INSTALL_MARKER_FILENAME
    if marker_path.exists():
        return
    marker_data = {
        "space_id": space_id,
        "install_path": install_path,
        "game_title": title,
    }
    try:
        marker_path.write_text(
            json.dumps(marker_data),
            encoding="utf-8",
        )
    except OSError:
        pass


class _DetectionHelpers:
    """Pure helpers attached to ``_InstallDetector`` for filesystem-side scans.

    Resolves the set of external game roots Unifideck should
    scan: configured defaults, SD card, user-overridden custom
    path, and any mounted media drives.
    """

    def __init__(self, parent: _InstallDetector) -> None:
        """Bind the detector-helpers to their parent install detector.

        Args:
            parent: Owning ``_InstallDetector`` instance.
        """
        self._parent = parent

    def get_external_game_roots(self) -> list[str]:
        """Compute the full list of external roots to scan for installed Ubisoft games.

        Combines the configured default install base, SD card,
        any user-overridden custom path, and every mounted
        ``/run/media`` drive. Roots are deduplicated by realpath
        before being returned.

        Returns:
            List of absolute paths (some may not exist yet).
        """
        config = self._parent._config
        roots: list[str] = [
            config.default_install_base_expanded,
            config.sdcard_install_base,
        ]
        self._append_custom_path_root(roots, config)
        self._append_mounted_media_roots(roots)
        return self._dedup_roots_by_realpath(roots)

    @staticmethod
    def _append_custom_path_root(
        roots: list[str],
        config: Any,
    ) -> None:
        """Append the user's custom-path override from download_settings.json.

        Args:
            roots: Output list (mutated).
            config: UbisoftConfig.
        """
        if config is None:
            return
        settings_file = str(Path(config.data_dir_expanded) / "download_settings.json")
        if not Path(settings_file).is_file():
            return
        settings = load_json_file_safe(settings_file)
        if not isinstance(settings, dict):
            return
        custom_path = settings.get("custom_path")
        if isinstance(custom_path, str) and custom_path:
            roots.append(
                str(Path(custom_path) / "Ubisoft"),
            )
            roots.append(custom_path)

    @staticmethod
    def _append_mounted_media_roots(roots: list[str]) -> None:
        """Append every plausible Games/Ubisoft path under ``/run/media``.

        Walks all first-level entries under ``/run/media`` (and
        their immediate children to cover
        ``/run/media/<user>/<drive>``).

        Args:
            roots: Output list (mutated).
        """
        media_base = Path("/run/media")
        if not media_base.is_dir():
            return
        try:
            for entry_path in media_base.iterdir():
                if not entry_path.is_dir():
                    continue
                roots.append(
                    str(entry_path / "Games" / "Ubisoft"),
                )
                _DetectionHelpers._append_sub_mount_roots(
                    entry_path,
                    roots,
                )
        except OSError:
            pass

    @staticmethod
    def _append_sub_mount_roots(
        parent: Path,
        roots: list[str],
    ) -> None:
        """Append ``Games/Ubisoft`` paths inside every subdir of ``parent``.

        Used to cover the case where ``/run/media`` directly contains
        user dirs containing the actual drives.

        Args:
            parent: Path under ``/run/media``.
            roots: Output list (mutated).
        """
        try:
            for sub_path in parent.iterdir():
                if sub_path.is_dir():
                    roots.append(
                        str(sub_path / "Games" / "Ubisoft"),
                    )
        except OSError:
            pass

    @staticmethod
    def _dedup_roots_by_realpath(
        roots: list[str],
    ) -> list[str]:
        """Deduplicate roots by their resolved real path.

        Preserves insertion order. Failures to resolve fall back
        to the raw path for comparison.

        Args:
            roots: Candidate list.

        Returns:
            Deduplicated list.
        """
        seen: set[str] = set()
        unique: list[str] = []
        for r in roots:
            try:
                real = str(Path(r).resolve())
            except OSError:
                real = r
            if real not in seen:
                seen.add(real)
                unique.append(r)
        return unique
