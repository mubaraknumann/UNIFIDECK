"""Filesystem packaging helpers — restores executable bits on shipped launcher binaries after extraction."""

from __future__ import annotations
import logging
import os
import stat
from collections.abc import Iterable
from pathlib import Path
logger = logging.getLogger(__name__)
LAUNCHER_EXECUTABLE_FILES: tuple[str, ...] = (
 "bin/unifideck-launcher",
)
_EXEC_MASK = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
def ensure_executable_files(
 plugin_dir: Path,
 files: Iterable[str] = LAUNCHER_EXECUTABLE_FILES,
) -> int:
    """Restore +x bits on shipped launcher binaries after extraction.

    Some plugin installation flows (notably Decky's ZIP
    extraction) strip the executable bit. Iterates the
    configured files under ``plugin_dir``, chmodding any that
    lack the world/group/user exec bits.

    Args:
        plugin_dir: Plugin root directory.
        files: Iterable of relative paths to fix
            (defaults to ``LAUNCHER_EXECUTABLE_FILES``).

    Returns:
        Number of files chmodded (already-executable files
        are not counted).
    """
    fixed = 0
    for rel_path in files:
        target = plugin_dir / rel_path
        if not target.is_file():
            logger.info(
                "[packaging] skipping missing file: %s",
                rel_path,
            )
            continue
        try:
            current_mode = target.stat().st_mode
        except OSError as e:
            logger.warning(
                "[packaging] cannot stat %s: %s",
                rel_path, e,
            )
            continue
        if current_mode & _EXEC_MASK:
            continue
        new_mode = current_mode | _EXEC_MASK
        try:
            os.chmod(target, new_mode)
            logger.info(
                "[packaging] fixed executable bit on %s "
                "(mode %#o → %#o)",
                rel_path,
                current_mode & 0o777, new_mode & 0o777,
            )
            fixed += 1
        except OSError as e:
            logger.warning(
                "[packaging] chmod failed on %s: %s",
                rel_path, e,
            )
    if fixed > 0:
        logger.info(
            "[packaging] executable bit self-heal: "
            "%d file(s) fixed",
            fixed,
        )
    return fixed