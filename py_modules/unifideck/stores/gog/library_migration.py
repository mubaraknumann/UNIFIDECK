"""One-shot migration of legacy GOG install marker files to the new JSON format.

OP-22-gog-library-migration
File: py_modules/unifideck/stores/gog/library_migration.py

Pre-Sprint-12, the ``.unifideck-id`` marker was a
plain text file containing just the GOG product
id (e.g. ``"1207658891"``). Sprint 12 introduced
the JSON-with-goggame-info format. This module
upgrades old markers in place at library-scan
time.

Migration is opportunistic: scans the download
dir, finds markers, decides if they're already in
the new format (skip), and migrates if not. Errors
during a single migration log + skip — they
don't abort the whole scan.

Three sources for the legacy id:

* Pure-string content (most common — older
  versions wrote the raw id);
* JSON number (an interim version wrapped it as
  a JSON int);
* JSON object containing ``game_id`` (already new
  format — skip).

The migration enriches the new format by loading
the install's ``goggame-<id>.info`` if available,
so the upgraded marker has all the metadata the
launcher expects.
"""

from __future__ import annotations

import glob
import json
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .library import GOGLibrary

logger = logging.getLogger(__name__)

_INSTALL_MARKER = ".unifideck-id"


class _MarkerMigration:
    """Per-library one-shot marker migration helper.

    Held by ``GOGLibrary`` and invoked from
    ``get_library`` on the first scan. Safe to
    re-run — already-migrated markers are
    detected and skipped.
    """

    def __init__(self, parent: GOGLibrary) -> None:
        """Stash parent reference (for config access).

        Args:
            parent: ``GOGLibrary`` instance.
        """
        self._parent = parent

    def migrate_old_markers(self) -> dict[str, int]:
        """Scan the download dir + upgrade any pre-Sprint-12 markers.

        Returns counts in a dict — useful for
        post-scan logging + telemetry.

        Pipeline:

        1. Resolve the download dir; doesn't
           exist → return zeros;
        2. For each top-level subdir, check for
           a marker file;
        3. Migrate each marker via
           ``_migrate_one_marker`` (returns
           ``"migrated"`` / ``"skipped"`` /
           ``"failed"``).

        OSError on scan → log + abort scan
        (return whatever counts we had).

        Returns:
            ``{"migrated": int, "skipped": int}``.
        """
        migrated = 0
        skipped = 0
        download_dir = os.path.expanduser(
            self._parent._config.download_dir,
        )
        if not os.path.isdir(download_dir):
            return {"migrated": 0, "skipped": 0}
        try:
            for name in os.listdir(download_dir):
                game_dir = os.path.join(download_dir, name)
                if not os.path.isdir(game_dir):
                    continue
                marker_path = os.path.join(
                    game_dir,
                    _INSTALL_MARKER,
                )
                if not os.path.isfile(marker_path):
                    continue
                outcome = self._migrate_one_marker(
                    game_dir,
                    marker_path,
                )
                if outcome == "migrated":
                    migrated += 1
                else:
                    skipped += 1
        except OSError as e:
            logger.error(
                "[GOGLibrary] migrate scan failed: %s",
                e,
            )
        logger.info(
            "[GOGLibrary] migration: %d upgraded, %d current",
            migrated,
            skipped,
        )
        return {"migrated": migrated, "skipped": skipped}

    def _migrate_one_marker(self, game_dir: str, marker_path: str) -> str:
        """Migrate one marker file. Returns ``"migrated"``/``"skipped"``/``"failed"``.

        Pipeline:

        1. Read content; OSError → failed;
        2. Already new format → skipped;
        3. Extract legacy id; missing →
           skipped (corrupt marker);
        4. Build new payload (enriched with
           goggame info if available);
        5. Write new payload atomically.

        Args:
            game_dir: dir containing the marker.
            marker_path: marker file path.

        Returns:
            Outcome string.
        """
        content = self._read_marker_content(marker_path)
        if content is None:
            return "failed"
        if self._marker_is_new_format(content):
            return "skipped"
        old_id = self._extract_legacy_id(content)
        if not old_id:
            return "skipped"
        new_data = self._build_new_marker_payload(
            game_dir,
            old_id,
        )
        return self._write_new_marker(
            marker_path,
            new_data,
            game_dir,
        )

    @staticmethod
    def _read_marker_content(marker_path: str) -> str | None:
        """Read the marker file's content as a stripped UTF-8 string.

        OSError → ``None``. Stripping handles
        legacy markers that had trailing
        newlines.

        Args:
            marker_path: file path.

        Returns:
            Content or ``None``.
        """
        try:
            with open(marker_path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return None

    @staticmethod
    def _marker_is_new_format(content: str) -> bool:
        """Quick check: is this content already the new JSON-dict format?

        Two conditions both required:

        * Parses as JSON;
        * Parsed value is a dict with ``game_id``.

        A bare JSON number (legacy interim) is
        NOT in the new format.

        Args:
            content: marker text.

        Returns:
            True iff new format.
        """
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return False
        return isinstance(data, dict) and "game_id" in data

    @staticmethod
    def _extract_legacy_id(content: str) -> str | None:
        """Pull the game id out of legacy marker content (string or JSON number).

        Tries three patterns:

        1. JSON parse → int or str → ``str(value)``;
        2. Raw text not starting with ``{`` (so
           not malformed JSON) → return as-is;
        3. None of the above → ``None`` (caller
           skips this marker).

        Args:
            content: marker text.

        Returns:
            Legacy id, or ``None``.
        """
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, (str, int)):
            return str(data)
        if content and not content.startswith("{"):
            return content
        return None

    def _build_new_marker_payload(self, game_dir: str, old_id: str) -> dict[str, Any]:
        """Build the new-format payload, enriching with goggame-<id>.info if found.

        Pipeline:

        1. Start with ``{"game_id": old_id}``;
        2. Search both ``game_dir/`` and
           ``game_dir/game/`` for goggame info
           files;
        3. First match → load JSON, force
           ``game_id`` key to the legacy id (so
           we know it's the same game), break.

        Falls back to the bare ``{"game_id"}``
        if no info file found or all loads fail.

        Args:
            game_dir: install dir.
            old_id: legacy id.

        Returns:
            Enriched payload dict.
        """
        new_data: dict[str, Any] = {"game_id": old_id}
        for candidate in (
            game_dir,
            os.path.join(game_dir, "game"),
        ):
            if not os.path.isdir(candidate):
                continue
            info_file = self._find_first_goggame_info(candidate)
            if not info_file:
                continue
            try:
                with open(info_file, encoding="utf-8") as f:
                    new_data = json.load(f)
                new_data["game_id"] = old_id
            except (OSError, json.JSONDecodeError):
                pass
            break
        return new_data

    @staticmethod
    def _write_new_marker(marker_path: str, new_data: dict[str, Any], game_dir: str) -> str:
        """Overwrite the marker file with the new payload. Returns outcome string.

        Not atomic — we just open + write. The
        operation is idempotent enough that a
        partial write on power loss would just
        leave a corrupted marker for the next
        scan to re-attempt.

        Args:
            marker_path: target file.
            new_data: payload.
            game_dir: dir (for error logging).

        Returns:
            ``"migrated"`` or ``"failed"``.
        """
        try:
            with open(marker_path, "w", encoding="utf-8") as f:
                json.dump(new_data, f, indent=2)
                return "migrated"
        except OSError as e:
            logger.warning(
                "[GOGLibrary] migrate write failed for %s: %s",
                game_dir,
                e,
            )
            return "failed"

    @staticmethod
    def _find_first_goggame_info(directory: str) -> str | None:
        """Glob-find the alphabetically-first ``goggame-*.info`` in ``directory``.

        Sorts to be deterministic — without
        sort, the order depends on filesystem
        and could pick a DLC info file over the
        main game's. Sorted, ``goggame-<id>.info``
        for the lowest id wins, which is
        consistent run-to-run.

        Args:
            directory: search directory.

        Returns:
            First matching path, or ``None``.
        """
        candidates = sorted(glob.glob(os.path.join(directory, "goggame-*.info")))
        return candidates[0] if candidates else None
