"""services/cloud_save/fs_ops.py — Filesystem primitives for cloud save sync.

Pure sync functions — the service runs them via
``asyncio.to_thread`` to avoid blocking the event loop. Kept
separate so ``service.py`` stays focused on orchestration
(manifest compare, conflict routing) rather than I/O mechanics.
"""
from __future__ import annotations

import logging
import os
import shutil

from .constants import MANIFEST_FILE
from pathlib import Path

logger = logging.getLogger(__name__)


def walk_mtimes(root: str) -> dict[str, float]:
    """Return a flat ``{relpath: mtime}`` map for files under ``root``.

    Skips dot-files and the manifest itself. Per-file OSError
    (file vanished mid-walk) is silently skipped — the caller
    gets a partial map which is still useful for diff.
    """
    mtimes = {}
    if not Path(root).is_dir():
        return mtimes

    for dirpath, _, files in Path(root).walk():
        for f in files:
            if f.startswith(".") or f == MANIFEST_FILE:
                continue

            path = str(Path(dirpath) / f)
            rel = str(Path(path).relative_to(root))
            try:
                mtimes[rel] = Path(path).stat().st_mtime
            except OSError:
                pass

    return mtimes


def copy_tree(
    src: str,
    dst: str,
    skip_manifest: bool = False,
) -> None:
    """Recursively copy ``src`` → ``dst`` preserving mtimes.

    Unlike ``shutil.copytree``, merges into an existing
    directory instead of failing. ``skip_manifest=True``
    excludes the manifest file — callers refresh it separately
    via ``write_manifest`` so the old manifest never gets
    copied forward with stale mtimes. Skips dot-files. Per-file
    OSError logged at DEBUG, copy continues.
    """
    if not Path(src).is_dir():
        return

    Path(dst).mkdir(parents=True, exist_ok=True)

    for dirpath, dirnames, files in Path(src).walk():
        rel_dir = str(Path(dirpath).relative_to(src))
        dst_dir = str(Path(dst) / rel_dir) if rel_dir != "." else dst

        Path(dst_dir).mkdir(parents=True, exist_ok=True)

        for f in files:
            if f.startswith("."):
                # Handle manifest skipping explicitly if it's dot-prefixed
                if skip_manifest and f == MANIFEST_FILE:
                    continue
                # The spec says "skips dot-files". But saves might use dot files?
                # We will follow the spec "Skips dot-files".
                continue

            src_file = str(Path(dirpath) / f)
            dst_file = str(Path(dst_dir) / f)

            try:
                shutil.copy2(src_file, dst_file)
            except OSError as e:
                logger.debug("[CloudSaveFsOps] failed to copy %s: %s", src_file, e)


def read_text(path: str) -> str:
    """Read ``path`` as UTF-8 text. Raises OSError on missing file."""
    with Path(path).open(encoding="utf-8") as f:
        return f.read()


def write_text(path: str, content: str) -> None:
    """Write ``content`` to ``path`` as UTF-8 text (overwrite)."""
    parent = str(Path(path).parent)
    if parent:
        Path(parent).mkdir(parents=True, exist_ok=True)
        
    with Path(path).open("w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
