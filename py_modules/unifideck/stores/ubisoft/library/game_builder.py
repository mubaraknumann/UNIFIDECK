"""
Build display-ready GameRecord entries from owned + installed data.

OP-57d | py_modules/unifideck/stores/ubisoft/library/game_builder.py

``_GameBuilder`` combines:

* the UPC catalog (owned-games + metadata);
* the install registry (installed-state);
* the id_map (UPC ↔ Unifideck IDs);
* the SteamGridDB artwork URLs (if cached);

into a uniform ``GameRecord`` shape consumed by the UI. The builder
applies normalisation rules (lowercase names for sort, strip trademark
glyphs, deduplicate when UPC reports a game under multiple space_ids)
and assigns each record a stable display order.
"""

from __future__ import annotations
import logging
import re
from typing import TYPE_CHECKING, Any
from ....core.types import Game
from ..steam_filter import filter_steam_linked_configs

if TYPE_CHECKING:
    from ..config import UbisoftConfig
    from ..id_map import UbisoftIdMap
    from ..parser import GameConfig
logger = logging.getLogger(__name__)
_MOJIBAKE_REPLACEMENTS = (
    ("Â®", "®"),
    ("â\u0080¢", "™"),
    ("â\u0084¢", "™"),
    ("â\u0080\u0099", "’"),
    ("Â", ""),
)
_SKIP_TITLE_KEYWORDS = re.compile(
    r"\b(test\b|beta|alpha|closed|preorder|pre-order|promotion|"
    r"internal|dev/qc|pts|test server|demo|trial)\b",
    re.IGNORECASE,
)
_SKIP_DLC_KEYWORDS = re.compile(
    r"\b(dlc|season pass|expansion|pack|bonus|soundtrack|"
    r"art ?book|skins?|outfit|costume|weapon|map|mission|"
    r"episode|revolver|kukri|cane-sword|hammer|knife|dagger|"
    r"conspiracy|runaway train|texture|language|starter edition|"
    r"battle pass|car shipment|full stock|full ownership|"
    r"master unlock|paint|perk|club|credit pack|currency pack|"
    r"ownership|ubicollectibles|legion of the dead|"
    r"calling all units)\b",
    re.IGNORECASE,
)
_STORE_MARKER_PATTERN = re.compile(
    r"\[STEAM\]|\[Uplay",
    re.IGNORECASE,
)
_CYRILLIC_PATTERN = re.compile(r"[\u0400-\u04FF]")
_PLACEHOLDER_L_PATTERN = re.compile(r"(l\d+|[A-Z0-9_]+)")
_PLACEHOLDER_LITERALS = frozenset({"a ubisoft game"})


class _GameBuilder:
    """Build display-ready ``Game`` records from UPC catalog + install state.

    Merges the parsed UPC owned-games catalog with the install
    registry, applies Steam-linked filtering, drops DLC / placeholder
    / non-Latin titles, deduplicates by normalized name, and
    updates the id_map in bulk to capture the latest mappings.
    """

    def __init__(
        self,
        *,
        config: UbisoftConfig,
        id_map: UbisoftIdMap,
    ) -> None:
        """Bind the game-builder to its config + id_map + library facade.

        Args:
            config: Frozen ``UbisoftConfig``.
            id_map: Space_id ↔ install_id mapping store.
            library: Owning ``UbisoftLibrary`` facade (for delegated
                artwork / metadata lookups).
        """
        self._config = config
        self._id_map = id_map

    @staticmethod
    def build_config_lookup(
        configs: list[GameConfig],
    ) -> dict[int, GameConfig]:
        """Index parsed configs by both install_id and launch_id.

        Args:
            configs: Parsed UPC owned-games entries.

        Returns:
            ``{numeric_id: config}`` covering install and launch IDs
            (when they differ).
        """
        config_by_id: dict[int, GameConfig] = {}
        for cfg in configs:
            config_by_id[cfg.install_id] = cfg
            if cfg.launch_id and cfg.launch_id != cfg.install_id:
                config_by_id[cfg.launch_id] = cfg
        return config_by_id

    @staticmethod
    def cross_reference_ownership(
        configs: list[GameConfig],
        config_by_id: dict[int, GameConfig],
        owned_set: set[int] | None,
    ) -> list[GameConfig]:
        """Filter the parsed configs against the ownership-binary owned set.

        When the ownership binary couldn't be loaded, falls back to
        every named config (less reliable but better than nothing).

        Args:
            configs: All parsed configs (untouched fallback list).
            config_by_id: Numeric-ID lookup from ``build_config_lookup``.
            owned_set: Numeric IDs from the ownership binary, or
                ``None`` to disable the cross-reference.

        Returns:
            Filtered list of configs the user actually owns.
        """
        if owned_set is not None:
            return [
                config_by_id[oid]
                for oid in owned_set
                if oid in config_by_id and config_by_id[oid].name
            ]
        result = [c for c in configs if c.name]
        logger.info(
            "[UbisoftLibrary] no ownership binary — using all %d config entries",
            len(result),
        )
        return result

    def apply_steam_filter(
        self,
        configs: list[GameConfig],
    ) -> list[GameConfig]:
        """Drop games already on Steam if cross-store filtering is enabled.

        Args:
            configs: Owned configs.

        Returns:
            Filtered list (unchanged if the toggle is off).
        """
        if not self._config.filter_steam_linked:
            return configs
        before_count = len(configs)
        result = self._filter_steam_linked_configs(configs)
        dropped = before_count - len(result)
        if dropped:
            logger.info(
                "[UbisoftLibrary] filtered %d Steam-linked game(s) from library",
                dropped,
            )
        return result

    def _filter_steam_linked_configs(
        self,
        configs: list[GameConfig],
    ) -> list[GameConfig]:
        """Dispatch to the shared steam_filter module.

        Args:
            configs: Owned configs.

        Returns:
            Configs not linked to Steam.
        """
        return filter_steam_linked_configs(
            configs,
            self._config.steam_library_cross_ref,
            self._id_map,
        )

    def build_games_from_configs(
        self,
        matched_configs: list[GameConfig],
        installed: dict[str, Any],
    ) -> list[Game]:
        """Build ``Game`` records from matched configs and the install map.

        Sorts alphabetically by lowered name, dedupes by normalized
        name, drops placeholder/blacklisted titles, and accumulates
        id_map updates for a single bulk write at the end.

        Args:
            matched_configs: Configs the user owns.
            installed: Per-space_id install metadata.

        Returns:
            Sorted, deduplicated list of ``Game`` records.
        """
        games: list[Game] = []
        seen_norms: set[str] = set()
        id_map_updates: dict[str, dict[str, Any]] = {}
        for cfg in sorted(
            matched_configs,
            key=lambda c: (c.name or "").lower(),
        ):
            game = self._build_one_game(
                cfg,
                installed,
                seen_norms,
                id_map_updates,
            )
            if game is not None:
                games.append(game)
        if id_map_updates:
            self._id_map.update_bulk(id_map_updates)
        return games

    def _build_one_game(
        self,
        cfg: GameConfig,
        installed: dict[str, Any],
        seen_norms: set[str],
        id_map_updates: dict[str, dict[str, Any]],
    ) -> Game | None:
        """Convert one config + install state into a ``Game`` record.

        Cleans the title, applies the skip filter, dedupes against
        ``seen_norms``, builds the id_map update, and assembles the
        final ``Game``.

        Args:
            cfg: UPC config entry.
            installed: Per-space_id install map.
            seen_norms: Set of already-emitted normalized names
                (mutated).
            id_map_updates: Accumulated id_map updates (mutated).

        Returns:
            A ``Game``, or ``None`` if the title should be skipped
            or is a duplicate.
        """
        title = self._clean_launcher_title(cfg.name)
        if self._should_skip_launcher_title(title):
            return None
        norm_name = self._id_map.normalize_for_matching(title)
        if norm_name in seen_norms:
            return None
        seen_norms.add(norm_name)
        game_id = cfg.space_id if cfg.space_id else str(cfg.install_id)
        is_installed = game_id in installed or cfg.space_id in installed
        install_meta = installed.get(game_id) or installed.get(cfg.space_id) or {}
        id_map_updates[game_id] = {
            "install_id": str(cfg.install_id),
            "launch_id": str(cfg.launch_id),
            "name": title,
            "executable": getattr(cfg, "executable", None),
            "game_identifier": getattr(
                cfg,
                "game_identifier",
                None,
            ),
            "source": "local_binary",
        }
        return Game(
            app_id=0,
            store="ubisoft",
            store_game_id=game_id,
            title=title,
            installed=is_installed,
            install_path=install_meta.get("install_path"),
            exe_path=install_meta.get("executable"),
            metadata={"ownership_type": "owned"},
        )

    @staticmethod
    def _clean_launcher_title(title: Any) -> str:
        """Strip surrounding quotes and fix common mojibake in a UPC title.

        Args:
            title: Raw title (any type — non-strings yield ``""``).

        Returns:
            Cleaned title.
        """
        if not isinstance(title, str):
            return ""
        cleaned = title.strip().strip('"').strip("'")
        for bad, good in _MOJIBAKE_REPLACEMENTS:
            cleaned = cleaned.replace(bad, good)
        return cleaned

    def _is_launcher_placeholder_title(self, title: str) -> bool:
        """Return True if the title is a known UPC placeholder.

        Matches: empty/blank strings, ``"a ubisoft game"``, and pure
        identifier-style strings (``L42``, ``GAME_FOO``).

        Args:
            title: Title to inspect.

        Returns:
            True iff the title should be hidden from the library.
        """
        cleaned = self._clean_launcher_title(title)
        if not cleaned:
            return True
        normalized = self._id_map.normalize_for_matching(
            cleaned,
        )
        if normalized in _PLACEHOLDER_LITERALS:
            return True
        return bool(_PLACEHOLDER_L_PATTERN.fullmatch(cleaned))

    def _should_skip_launcher_title(self, title: str) -> bool:
        """Composite skip predicate covering every drop reason.

        Drops: empty/too-short titles, placeholders, anything with
        ``[STEAM]`` / ``[Uplay`` markers, test/beta/DLC keywords,
        or Cyrillic characters (placeholder Russian rows in the
        config).

        Args:
            title: Cleaned title.

        Returns:
            True iff the title should not appear in the library.
        """
        cleaned = self._clean_launcher_title(title)
        if not cleaned or len(cleaned.strip()) <= 2:
            return True
        if self._is_launcher_placeholder_title(cleaned):
            return True
        if _STORE_MARKER_PATTERN.search(cleaned):
            return True
        if _SKIP_TITLE_KEYWORDS.search(cleaned):
            return True
        if _CYRILLIC_PATTERN.search(cleaned):
            return True
        return bool(_SKIP_DLC_KEYWORDS.search(cleaned))
