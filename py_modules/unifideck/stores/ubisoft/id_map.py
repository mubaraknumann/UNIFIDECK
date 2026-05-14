"""
UPC ID ↔ Unifideck install ID mapping — persistent on-disk store.

OP-55g | py_modules/unifideck/stores/ubisoft/id_map.py

UPC identifies a game by its ``space_id`` (a GUID-like string), but
Unifideck uses a stable ``install_id`` for shortcuts, save-management,
and cross-store correlation. ``UbisoftIdMap`` is the bidirectional
lookup table between the two.

It's persisted as JSON at ``UbisoftConfig.id_map_file_expanded`` and
written atomically (temp-file + ``os.replace``) so a crash during save
can't leave the table in a partial state. Reads are eager (loaded once
at construction) and writes flush after every mutation.

The class also resolves *partial* IDs (e.g. when only the install path
is known) by walking the local install directory and looking for
``goggame-style`` markers or extracted .info files.
"""

from __future__ import annotations
import json
import logging
import os
import re
from typing import Any
from .config import UbisoftConfig
from .id_map_sources import (
    _IdMapSources,
    extract_game_id_from_registry as _extract_game_id_from_registry,
)
from .paths import UbisoftPrefixPaths

logger = logging.getLogger(__name__)
_STEAM_TITLE_PREFIXES_TO_SKIP = (
    "Proton",
    "Steam Linux Runtime",
    "Steamworks",
)


class UbisoftIdMap:
    """Persistent UPC space_id ↔ Unifideck install_id bidirectional lookup table.

    Loaded eagerly at construction from the on-disk JSON cache and
    written atomically (temp + ``os.replace``) after every mutation.
    Also resolves partial IDs by scanning the local install directory
    and walking the configurations cache via ``_IdMapSources``.
    """

    def __init__(
        self,
        config: UbisoftConfig,
        paths: UbisoftPrefixPaths,
    ) -> None:
        """Build the ID-map cache from the on-disk map file.

        Loads the persisted ``space_id ↔ install_id ↔ launch_id``
        mapping and wires the source helpers used to enrich it on
        demand.

        Args:
            config: Ubisoft store config.
            paths: Ubisoft prefix paths (holds the map file location).
        """
        self._config = config
        self._paths = paths
        self._cache: dict[str, dict[str, Any]] = {}
        self._load()
        self._sources = _IdMapSources(self)

    def _load(self) -> None:
        """Read the JSON cache from disk into ``self._cache`` (best-effort).

        No-op if the file doesn't exist. Read/decode errors fall back
        to an empty cache with a warning.
        """
        path = self._config.id_map_file_expanded
        if not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._cache = data
                logger.info(
                    "[UbisoftIdMap] loaded %d entries from cache",
                    len(self._cache),
                )
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "[UbisoftIdMap] could not load cache: %s",
                e,
            )
            self._cache = {}

    def _save(self) -> None:
        """Atomically persist the in-memory cache to disk.

        Writes to a ``.tmp`` sibling first, then ``os.replace`` into
        place. Failures are logged but not raised.
        """
        path = self._config.id_map_file_expanded
        try:
            os.makedirs(
                self._config.data_dir_expanded,
                exist_ok=True,
            )
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2)
                os.replace(tmp_path, path)
        except OSError as e:
            logger.warning(
                "[UbisoftIdMap] could not save cache: %s",
                e,
            )

    def resolve_install_id(
        self,
        space_id: str,
    ) -> str | None:
        """Return the persistent install ID associated with a UPC space_id.

        Prefers the newer ``ubisoftconnect_game_id`` key when present;
        otherwise falls back to the legacy ``install_id`` key.

        Args:
            space_id: UPC space_id (GUID-like string).

        Returns:
            The install ID, or ``None`` if the space_id is unknown.
        """
        entry = self._cache.get(space_id, {})
        if "ubisoftconnect_game_id" in entry:
            return entry.get("ubisoftconnect_game_id")
        return entry.get("install_id")

    def resolve_launch_id(
        self,
        space_id: str,
    ) -> str | None:
        """Return the launch ID associated with a UPC space_id.

        Prefers the newer ``ubisoftconnect_game_id`` key when present;
        otherwise falls back to the legacy ``launch_id`` key.

        Args:
            space_id: UPC space_id.

        Returns:
            The launch ID, or ``None`` if the space_id is unknown.
        """
        entry = self._cache.get(space_id, {})
        if "ubisoftconnect_game_id" in entry:
            return entry.get("ubisoftconnect_game_id")
        return entry.get("launch_id")

    def update(
        self,
        space_id: str,
        install_id: str,
        launch_id: str,
    ) -> None:
        """Overwrite the entry for one space_id and persist.

        Replaces any existing entry — use ``merge_entry`` to preserve
        other keys.

        Args:
            space_id: UPC space_id.
            install_id: Persistent install identifier.
            launch_id: Launch identifier (UPC URL fragment).
        """
        self._cache[space_id] = {
            "install_id": install_id,
            "launch_id": launch_id,
        }
        self._save()

    def update_bulk(
        self,
        mapping: dict[str, dict[str, Any]],
    ) -> None:
        """Merge a batch of entries into the cache; persist once if any changed.

        Per-space_id semantics: new keys are added, existing keys are
        overwritten by the supplied values, untouched keys are kept.

        Args:
            mapping: ``{space_id: fields_dict}``.
        """
        changed = False
        for space_id, entry in mapping.items():
            existing = self._cache.get(space_id, {})
            merged = {**existing, **entry}
            if merged != existing:
                self._cache[space_id] = merged
                changed = True
                if changed:
                    self._save()

    def merge_entry(
        self,
        space_id: str,
        fields: dict[str, Any],
    ) -> bool:
        """Shallow-merge ``fields`` into the entry for ``space_id``; save if it changed.

        Args:
            space_id: UPC space_id.
            fields: Fields to merge into the existing entry.

        Returns:
            True iff anything actually changed and was persisted.
        """
        existing = self._cache.get(space_id, {})
        merged = {**existing, **fields}
        if merged == existing:
            return False
        self._cache[space_id] = merged
        self._save()
        return True

    def get_entry(
        self,
        space_id: str,
    ) -> dict[str, Any]:
        """Return a defensive copy of the entry for one space_id.

        Args:
            space_id: UPC space_id.

        Returns:
            A copy of the entry dict (empty dict if unknown).
        """
        return dict(self._cache.get(space_id, {}))

    def in_cache(self, space_id: str) -> bool:
        """Check whether a space_id has a cached entry.

        Args:
            space_id: UPC space_id.

        Returns:
            True iff the space_id is present in the cache.
        """
        return space_id in self._cache

    async def refresh_from_configurations(
        self,
        space_id: str | None = None,
    ) -> bool:
        """Refresh entries by scanning the UPC ``configurations`` cache.

        Delegates to ``_IdMapSources``. When ``space_id`` is provided,
        only that entry is refreshed; otherwise every visible cache
        entry is rebuilt.

        Args:
            space_id: Optional single space_id to target.

        Returns:
            True iff at least one entry was updated.
        """
        return await self._sources.refresh_from_configurations(
            space_id,
        )

    async def fetch_game_id_database(
        self,
    ) -> list[tuple[str, str]]:
        """Fetch (and cache) the iArtorias GitHub UPC game-ID database.

        Delegates to ``_IdMapSources``. Refreshes when the local file
        is older than ``UbisoftConfig.game_id_db_max_age_seconds``.

        Returns:
            List of ``(game_name, game_id)`` tuples.
        """
        return await self._sources.fetch_game_id_database()

    async def lookup_game_id_by_name(
        self,
        game_name: str,
    ) -> str | None:
        """Resolve a UPC game ID by (normalized) game name.

        Delegates to ``_IdMapSources``.

        Args:
            game_name: Display name to look up.

        Returns:
            UPC game ID string, or ``None`` if no name match.
        """
        return await self._sources.lookup_game_id_by_name(
            game_name,
        )

    @staticmethod
    def extract_game_id_from_registry(
        prefix_path: str,
    ) -> str | None:
        """Pull the UPC install ID out of the prefix's system.reg.

        Delegates to ``id_map_sources.extract_game_id_from_registry``.

        Args:
            prefix_path: Path to the Wine prefix.

        Returns:
            The install ID, or ``None`` if no UPC install key is found.
        """
        return _extract_game_id_from_registry(prefix_path)

    @staticmethod
    def get_steam_library_titles() -> set[str]:
        """Return the user's Steam library titles, normalized for matching.

        Filters out Proton / Steam Linux Runtime / Steamworks entries
        that aren't real games. Returns an empty set when the Steam
        library module isn't importable or scanning fails.

        Returns:
            Normalized title set suitable for cross-reference matching.
        """
        try:
            from ...steam.library import get_steam_library_names
        except ImportError:
            logger.debug(
                "[UbisoftIdMap] Steam library module not available",
            )
            return set()
        try:
            raw_names = get_steam_library_names()
        except Exception as e:
            logger.debug(
                "[UbisoftIdMap] Steam library scan failed: %s",
                e,
            )
            return set()
        steam_titles: set[str] = set()
        for name in raw_names:
            if not name or name.startswith(
                _STEAM_TITLE_PREFIXES_TO_SKIP,
            ):
                continue
            steam_titles.add(
                UbisoftIdMap._normalize_for_matching(name),
            )
        logger.debug(
            "[UbisoftIdMap] found %d Steam library titles",
            len(steam_titles),
        )
        return steam_titles

    @staticmethod
    def _normalize_for_matching(name: str) -> str:
        """Normalize a title for fuzzy match against the Steam library.

        Lowercases, replaces ``_`` with space, strips trademark/punctuation
        characters, and collapses whitespace runs.

        Args:
            name: Raw title.

        Returns:
            Normalized form for case-insensitive equality.
        """
        name = name.lower()
        name = name.replace("_", " ")
        name = re.sub(
            r"[®™©''\u2019\-:.,!?()\"']",
            "",
            name,
        )
        return " ".join(name.split())

    def normalize_for_matching(self, name: str) -> str:
        """Instance-method alias for ``_normalize_for_matching``.

        Args:
            name: Raw title.

        Returns:
            Normalized form.
        """
        return self._normalize_for_matching(name)
