"""Locate the launchable executable in a GOG install (multi-strategy).

OP-22-gog-exe-resolver | py_modules/unifideck/stores/gog/exe_resolver.py

GOG installs are inconsistent — Windows games via
Wine/Proton, native Linux games via shell scripts,
DOSBox/ScummVM-wrapped classics with batch
launchers. This module picks the right entry
point.

Resolution strategy (in order):

1. **goggame info** — parse
   ``goggame-<id>.info`` for the ``isPrimary``
   playTask; this is the authoritative source
   gogdl uses;
2. **Wrapper batch** — if the playTask points at
   a DOSBox/ScummVM wrapper, check for
   ``run-game.bat`` and prefer that;
3. **Workdir override** — if data files like
   ``.arch05`` / ``.forge`` are in the install
   root, override the playTask workdir to point
   there (some GOG releases ship data in unusual
   locations);
4. **start.sh** — for native Linux installs;
5. **Largest exe** — last resort: scan *.exe,
   skip installers/redists/crash handlers, pick
   the largest remaining.

The ``_SKIP_EXE_PATTERNS`` list filters out
uninstall/setup/crash-handler exes that would
otherwise win the size race. ``_WRAPPER_EXE_NAMES``
identifies DOSBox/ScummVM wrappers.
"""

from __future__ import annotations

import glob
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SKIP_EXE_PATTERNS = (
    "unins",
    "setup",
    "install",
    "crash",
    "redist",
    "vcredist",
    "vc_redist",
    "dxsetup",
    "physx",
    "dotnet",
    "directx",
)
_ROOT_DATA_EXTENSIONS = (".arch05", ".forge")
_WRAPPER_EXE_NAMES = {"dosbox.exe", "scummvm.exe"}


class GOGExeResolver:
    """Find the launchable exe for a GOG install — multi-strategy resolver.

    Stateless — every method is either static or
    operates on its arguments only. Callers
    instantiate once and reuse.
    """

    def find(self, install_path: str) -> str | None:
        """Convenience wrapper — return just the exe path (drop workdir).

        Used when the caller doesn't care about
        the workdir (e.g. just checking if an
        exe exists for verification).

        Args:
            install_path: install root.

        Returns:
            Exe path or ``None``.
        """
        result = self.find_with_workdir(install_path)
        return result[0] if result else None

    def find_with_workdir(self, install_path: str) -> tuple[str, str] | None:
        """Full resolver — return (exe, workdir) tuple or ``None``.

        Catches any unexpected exception (the
        multi-strategy code is complex enough
        that a corrupt info file could throw in
        a path we haven't anticipated). Errors
        log + return ``None`` so launch fails
        cleanly rather than corrupting state.

        Args:
            install_path: install root.

        Returns:
            ``(exe, workdir)`` or ``None``.
        """
        try:
            return self._resolve(install_path)
        except Exception as e:
            logger.exception(
                "[GOGExeResolver] unexpected error for %s: %s",
                install_path,
                e,
            )
            return None

    def _resolve(self, install_path: str) -> tuple[str, str] | None:
        """Try each strategy in priority order; return the first match.

        Strategies tried:

        1. goggame info playTask;
        2. start.sh (native Linux);
        3. Largest exe (fallback).

        Returns ``None`` only if all three fail
        — that's a real failure (install
        corrupted or unrecognised game layout).

        Args:
            install_path: install root.

        Returns:
            ``(exe, workdir)`` or ``None``.
        """
        search_dirs = self._build_search_dirs(install_path)
        info_result = self._resolve_via_goggame_info(
            install_path,
            search_dirs,
        )
        if info_result:
            return info_result
        start_sh_result = self._resolve_via_start_sh(search_dirs)
        if start_sh_result:
            return start_sh_result
        fallback_result = self._resolve_via_largest_exe(
            search_dirs,
        )
        if fallback_result:
            return fallback_result
        logger.warning(
            "[GOGExeResolver] no executable found in %s",
            search_dirs,
        )
        return None

    @staticmethod
    def _build_search_dirs(install_path: str) -> list[str]:
        """Compute the ordered list of dirs to search for executables.

        Order matters: try ``install_path/game/``
        first (the common GOG layout), then
        ``install_path/`` itself. If
        ``game/`` doesn't exist, just the root
        is searched.

        Args:
            install_path: install root.

        Returns:
            List of directory paths.
        """
        search_dirs: list[str] = []
        game_subdir = str(Path(install_path) / "game")
        if Path(game_subdir).is_dir():
            search_dirs.append(game_subdir)
        search_dirs.append(install_path)
        return search_dirs

    def _resolve_via_goggame_info(self, install_path: str, search_dirs: list[str]) -> tuple[str, str] | None:
        """Strategy 1 — read goggame-<id>.info's primary playTask.

        Sub-steps:

        1. Find + load the info file;
        2. Pull the ``isPrimary`` playTask;
        3. Check for wrapper override (DOSBox /
           ScummVM batch launcher);
        4. Resolve absolute exe + workdir paths;
        5. Apply the data-files workdir override
           heuristic.

        Args:
            install_path: install root.
            search_dirs: directories to search.

        Returns:
            ``(exe, workdir)`` or ``None``.
        """
        primary, root_dir = self._load_primary_play_task(
            search_dirs,
        )
        if primary is None:
            return None
        wrapper = self._check_wrapper_override(
            install_path,
            root_dir,
            primary,
        )
        if wrapper:
            return wrapper
        return self._resolve_play_task_paths(
            install_path,
            root_dir,
            primary,
        )

    def _load_primary_play_task(self, search_dirs: list[str]) -> tuple[dict[str, Any] | None, str]:
        """Load the info file and pull out the ``isPrimary`` playTask.

        gogdl's info file has a ``playTasks``
        array; one or more are marked
        ``isPrimary: true`` (the main entry
        point) and others are bonus content
        (manuals, soundtracks).

        We want the first primary one. Returns
        ``(None, "")`` if no info file, parse
        fails, or no primary task.

        Args:
            search_dirs: where to look for info.

        Returns:
            ``(primary_task_dict_or_None,
            root_dir)``.
        """
        info_file, root_dir = self._find_goggame_info(search_dirs)
        if not info_file:
            return None, ""
        try:
            data = json.loads(
                Path(info_file).read_text(encoding="utf-8"),
            )
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "[GOGExeResolver] info file read failed: %s",
                e,
            )
            return None, ""
        play_tasks = data.get("playTasks", [])
        if not isinstance(play_tasks, list):
            return None, ""
        primary = next(
            (t for t in play_tasks if isinstance(t, dict) and t.get("isPrimary")),
            None,
        )
        return primary, root_dir

    def _resolve_play_task_paths(self, install_path: str, root_dir: str, primary: dict[str, Any]) -> tuple[str, str] | None:
        """Convert playTask path + workingDir to absolute filesystem paths.

        Both ``path`` and ``workingDir`` in the
        info file are relative to the goggame
        info's directory (``root_dir``). Windows
        paths use backslashes; we normalise to
        forward slashes for cross-platform
        consistency.

        Data-files override: if ``.arch05`` /
        ``.forge`` files exist in
        ``install_path``, force the workdir to
        ``install_path`` (some GOG releases ship
        data files at the install root rather
        than in the ``game/`` subdir, and the
        playTask's relative workdir gets it
        wrong).

        Args:
            install_path: install root.
            root_dir: goggame info's directory.
            primary: playTask dict.

        Returns:
            ``(exe, workdir)`` or ``None``.
        """
        exe_rel = primary.get("path", "")
        if not exe_rel:
            return None
        work_rel = primary.get("workingDir", "")
        full_exe = str(
            Path(root_dir) / exe_rel,
        ).replace("\\", "/")
        full_work = (
            str(Path(root_dir) / work_rel).replace("\\", "/")
            if work_rel
            else str(Path(full_exe).parent)
        )
        if full_work != install_path and self._has_root_data_files(install_path):
            logger.info(
                "[GOGExeResolver] data files in root, overriding workdir to %s",
                install_path,
            )
            full_work = install_path
        if not Path(full_exe).is_file():
            return None
        logger.info(
            "[GOGExeResolver] resolved via goggame info: %s",
            full_exe,
        )
        return (full_exe, full_work)

    @staticmethod
    def _find_goggame_info(search_dirs: list[str]) -> tuple[str | None, str]:
        """Find the first ``goggame-*.info`` file across the search dirs.

        Returns the directory where it was found
        as ``root_dir`` — that's where relative
        paths in the info file should be
        resolved against.

        Args:
            search_dirs: dirs to scan.

        Returns:
            ``(file_path_or_None, root_dir)``.
        """
        for directory in search_dirs:
            if not Path(directory).is_dir():
                continue
            try:
                for item in os.listdir(directory):
                    if item.startswith("goggame-") and item.endswith(".info"):
                        return (
                            str(Path(directory) / item),
                            directory,
                        )
            except OSError:
                continue
        return (None, search_dirs[0] if search_dirs else "")

    def _check_wrapper_override(self, install_path: str, root_dir: str, primary_task: dict[str, Any]) -> tuple[str, str] | None:
        """Prefer ``run-game.bat`` if the playTask points at a DOSBox/ScummVM wrapper.

        Two trigger conditions:

        1. The task's exe basename is in
           ``_WRAPPER_EXE_NAMES`` (i.e.
           dosbox.exe / scummvm.exe);
        2. Or the run-game.bat content
           references the task exe (case-
           insensitive) — handles less common
           wrapper configurations.

        Wrapper batches set up environment and
        DOS paths that the raw exe needs. Using
        them is more reliable than running the
        bare wrapper exe.

        Args:
            install_path: install root.
            root_dir: info file dir.
            primary_task: playTask dict.

        Returns:
            ``(wrapper_path, candidate_root)``
            or ``None``.
        """
        task_path = primary_task.get("path", "")
        if not task_path:
            return None
        task_basename = Path(task_path).name.lower()
        candidates = [root_dir]
        if install_path not in candidates:
            candidates.append(install_path)
        for candidate_root in candidates:
            wrapper_path = str(
                Path(candidate_root) / "run-game.bat",
            )
            if not Path(wrapper_path).is_file():
                continue
            try:
                content = (
                    Path(wrapper_path)
                    .read_text(
                        encoding="utf-8",
                        errors="ignore",
                    )
                    .lower()
                )
            except OSError:
                content = ""
            if task_basename in content or task_basename in _WRAPPER_EXE_NAMES:
                logger.info(
                    "[GOGExeResolver] using wrapper: %s",
                    wrapper_path,
                )
                return (wrapper_path, candidate_root)
        return None

    @staticmethod
    def _has_root_data_files(install_path: str) -> bool:
        """Detect data files at install root — triggers workdir override.

        Looks for files ending in
        ``.arch05`` / ``.forge`` — Anvil-engine
        game data containers (Assassin's Creed
        series and similar). If present, the
        game expects to run with ``install_path``
        as workdir.

        Args:
            install_path: install root.

        Returns:
            True iff a root data file exists.
        """
        try:
            for name in os.listdir(install_path):
                full = Path(install_path) / name
                if not full.is_file():
                    continue
                if any(name.endswith(ext) for ext in _ROOT_DATA_EXTENSIONS):
                    return True
        except OSError:
            pass
        return False

    @staticmethod
    def _resolve_via_start_sh(search_dirs: list[str]) -> tuple[str, str] | None:
        """Strategy 2 — native Linux ``start.sh`` script.

        GOG's native Linux installs always include
        a ``start.sh`` at the root. Workdir =
        same directory.

        Args:
            search_dirs: dirs to scan.

        Returns:
            ``(script_path, dir)`` or ``None``.
        """
        for directory in search_dirs:
            if not Path(directory).is_dir():
                continue
            candidate = str(Path(directory) / "start.sh")
            if Path(candidate).is_file():
                logger.info(
                    "[GOGExeResolver] resolved via start.sh: %s",
                    candidate,
                )
                return (candidate, directory)
        return None

    @staticmethod
    def _resolve_via_largest_exe(search_dirs: list[str]) -> tuple[str, str] | None:
        """Strategy 3 (fallback) — pick the largest non-installer ``.exe``.

        Recursive glob via ``**/*.exe`` plus
        top-level ``*.exe``. Filters out
        installers / redists / crash handlers
        via ``_SKIP_EXE_PATTERNS``. Picks the
        largest remaining — game exes are
        typically tens of MB, helpers are KB-
        range, so size is a decent heuristic.

        Returns ``(exe, dir)`` for the first
        search dir that yields any candidates.

        Args:
            search_dirs: dirs to scan.

        Returns:
            ``(exe, workdir)`` or ``None``.
        """
        for directory in search_dirs:
            if not Path(directory).is_dir():
                continue
            candidates: list[tuple[str, int]] = []
            for pattern in ("*.exe", "**/*.exe"):
                for exe_path in glob.glob(
                    str(Path(directory) / pattern),
                    recursive=True,
                ):
                    basename = Path(exe_path).name.lower()
                    if any(skip in basename for skip in _SKIP_EXE_PATTERNS):
                        continue
                    try:
                        candidates.append(
                            (
                                exe_path,
                                Path(exe_path).stat().st_size,
                            ),
                        )
                    except OSError:
                        continue
            if not candidates:
                continue
            candidates.sort(key=lambda item: item[1], reverse=True)
            best_exe, best_size = candidates[0]
            logger.info(
                "[GOGExeResolver] fallback: largest exe (%.1f MB): %s",
                best_size / (1024 * 1024),
                best_exe,
            )
            return (best_exe, str(Path(best_exe).parent))
        return None


def parse_size_string(size_str: str) -> int:
    """Parse a "N.N XB" string (GB/MB/KB) into bytes.

    GOG's API sometimes returns sizes as
    human-readable strings ("4.2 GB") rather
    than byte counts. This util normalises.

    Returns 0 on any parse failure (malformed
    input, unrecognised unit). Callers should
    treat 0 as "unknown" rather than empty.

    Args:
        size_str: human-readable size.

    Returns:
        Size in bytes, or 0.
    """
    if not size_str:
        return 0
    try:
        parts = str(size_str).strip().split()
        if len(parts) != 2:
            return 0
        value = float(parts[0])
        unit = parts[1].upper()
        if unit == "GB":
            return int(value * 1024 * 1024 * 1024)
        if unit == "MB":
            return int(value * 1024 * 1024)
        if unit == "KB":
            return int(value * 1024)
        return int(value)
    except (ValueError, TypeError):
        return 0


def get_game_id_from_goggame_filename(filename: str) -> str | None:
    """Extract the GOG product id from a ``goggame-<id>.info`` filename.

    Returns ``None`` on:

    * Empty input;
    * Doesn't start with ``goggame-`` or doesn't
      end with ``.info``;
    * Empty id after stripping prefix/suffix.

    Args:
        filename: filename (basename, not path).

    Returns:
        Product id string, or ``None``.
    """
    if not filename:
        return None
    name = filename.strip()
    if not name.startswith("goggame-") or not name.endswith(".info"):
        return None
    game_id = name[len("goggame-") : -len(".info")]
    return game_id if game_id else None
