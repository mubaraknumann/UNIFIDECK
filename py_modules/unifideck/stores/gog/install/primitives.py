"""Low-level folder operations used by the GOG install pipeline.

OP-22-gog-install-primitives
File: py_modules/unifideck/stores/gog/install/primitives.py

Static utilities collected on ``GOGFolderOps``:

* ``folder_size`` — recursive byte count;
* ``count_files`` — recursive file count;
* ``has_goggame_info`` — detect a partial/complete
  GOG install by the presence of
  ``goggame-<id>.info``;
* ``force_cleanup_folder`` — best-effort recursive
  remove (async wrapper around blocking I/O).

These all swallow ``OSError`` and return safe
defaults — install pipelines need them to never
throw because they run during progress reporting
where exceptions would corrupt the UI state.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


class GOGFolderOps:
    """Static-only collection of folder operations for the install pipeline.

    No instance state — every method is
    ``@staticmethod``. The class exists as a
    namespace so callers see ``GOGFolderOps.folder_size``
    rather than free functions.
    """

    @staticmethod
    def folder_size(path: str) -> int:
        """Sum the byte sizes of every regular file under ``path``.

        Per-file ``OSError`` (broken symlink,
        permission denied) is skipped; the top-
        level ``os.walk`` ``OSError`` (path
        doesn't exist) → return 0.

        Args:
            path: root directory.

        Returns:
            Total bytes (0 on missing path).
        """
        total = 0
        try:
            for root, _dirs, files in os.walk(path):
                for name in files:
                    try:
                        total += os.path.getsize(
                            os.path.join(root, name),
                        )
                    except OSError:
                        continue
        except OSError:
            pass
        return total

    @staticmethod
    def count_files(path: str) -> int:
        """Count regular files under ``path`` (recursive).

        OSError → 0. Used for progress-percentage
        sanity checks (no point reporting 50%
        progress on an install with 0 files).

        Args:
            path: root directory.

        Returns:
            File count (0 on missing path).
        """
        count = 0
        try:
            for _root, _dirs, files in os.walk(path):
                count += len(files)
        except OSError:
            pass
        return count

    @staticmethod
    def has_goggame_info(path: str, game_id: str = "") -> bool:
        """Detect a (partial or complete) GOG install via ``goggame-<id>.info``.

        gogdl writes ``goggame-<id>.info`` files
        to mark which products are installed in
        a given folder. Presence of *any*
        ``goggame-*.info`` is enough to say
        "something's installed here"; if
        ``game_id`` is provided, matches only
        that specific id.

        Args:
            path: install folder.
            game_id: optional specific id to
                match. Empty string matches any
                goggame.

        Returns:
            True iff a matching file exists.
        """
        try:
            for name in os.listdir(path):
                if not name.startswith("goggame-"):
                    continue
                if not name.endswith(".info"):
                    continue
                if not game_id:
                    return True
                if name == f"goggame-{game_id}.info":
                    return True
        except OSError:
            pass
        return False

    @staticmethod
    async def force_cleanup_folder(path: str) -> None:
        """Best-effort recursive remove — used when gogdl fails to clean up.

        ``topdown=False`` walks bottom-up so we
        can rmdir each level after emptying it.
        Per-file errors are logged at DEBUG (not
        WARN — these happen often during
        partial-install rollback and aren't
        actionable). Final ``rmdir`` on the root
        is attempted but silently skipped on
        failure.

        Runs in a worker thread via
        ``asyncio.to_thread`` since recursive
        deletes can be slow on spinning disks.

        Args:
            path: root to remove.
        """

        def _sync_cleanup() -> None:
            """Blocking recursive remove, counts deletions + errors.

            Logs a single INFO line at the end
            with the deletion counts for
            post-mortem analysis.
            """
            deleted = 0
            errors = 0
            for root, dirs, files in os.walk(path, topdown=False):
                for name in files:
                    try:
                        os.remove(os.path.join(root, name))
                        deleted += 1
                    except OSError as e:
                        logger.debug(
                            "[GOGFolderOps] could not remove %s: %s",
                            name,
                            e,
                        )
                        errors += 1
                for name in dirs:
                    try:
                        os.rmdir(os.path.join(root, name))
                    except OSError:
                        pass
            try:
                os.rmdir(path)
            except OSError:
                pass
            logger.info(
                "[GOGFolderOps] force cleanup: %d deleted, %d errors",
                deleted,
                errors,
            )

        await asyncio.to_thread(_sync_cleanup)
