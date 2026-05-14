"""
Fetch the owned-games catalog from the UPC user data.

OP-57b | py_modules/unifideck/stores/ubisoft/library/fetch.py

``_LibraryFetch`` reads the UPC catalog from the user's Wine prefix
(``ownership`` and ``configurations`` directories) and returns the
parsed owned-games list. Delegates to ``parser.py`` and
``parser_binary.py`` for the actual decoding.

Errors during read are surfaced as empty results — the caller will fall
back to "installed games only" mode if the owned list can't be read.
"""

from __future__ import annotations
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from ....core.types import Game
from .data_loader import _DataLoader
from .game_builder import _GameBuilder

if TYPE_CHECKING:
    from ..config import UbisoftConfig
    from ..id_map import UbisoftIdMap
    from ..parser import GameConfig
    from ..paths import UbisoftPrefixPaths
ParseConfigurationsFn = Callable[[str], "list[GameConfig]"]
ParseOwnershipFn = Callable[[str], list[int]]
logger = logging.getLogger(__name__)


class _LibraryFetcher:
    """Fetch the owned-games catalog from UPC's local binaries.

    Orchestrates the data loader (configurations + ownership) and
    the game builder (filtering, deduplication). Returns ``None``
    from ``fetch_local_binaries`` when no usable data is on disk
    yet (typically before first auth).
    """

    def __init__(
        self,
        config: UbisoftConfig,
        paths: UbisoftPrefixPaths,
        id_map: UbisoftIdMap,
    ) -> None:
        """Build the library fetcher with its loader and game-builder sub-objects.

        Args:
            config: Ubisoft store config.
            paths: Ubisoft prefix paths (cache locations).
            id_map: Ubisoft ID map (used to enrich raw rows with
                canonical IDs).
        """
        self._config = config
        self._paths = paths
        self._id_map = id_map
        self._loader = _DataLoader(config=config, paths=paths)
        self._builder = _GameBuilder(
            config=config,
            id_map=id_map,
        )

    async def fetch_local_binaries(
        self,
        installed: dict[str, Any],
    ) -> list[Game] | None:
        """Load and build the owned-games list from UPC's local binaries.

        Pipeline: import parser → load configurations → load ownership
        → build per-ID lookup → cross-reference ownership → filter
        Steam-linked → build display-ready ``Game`` records.

        Args:
            installed: Per-space_id install metadata (drives the
                ``installed=True`` flag on records).

        Returns:
            List of ``Game`` records, or ``None`` if any required
            source is missing.
        """
        parser_funcs = self._import_ubisoft_parser()
        if parser_funcs is None:
            return None
        parse_configurations, parse_ownership = parser_funcs
        configs = await self._loader.load_configurations(
            parse_configurations,
        )
        if not configs:
            return None
        owned_set = await self._loader.load_ownership_set(
            parse_ownership,
        )
        config_by_id = self._builder.build_config_lookup(configs)
        matched_configs = self._builder.cross_reference_ownership(
            configs,
            config_by_id,
            owned_set,
        )
        matched_configs = self._builder.apply_steam_filter(
            matched_configs,
        )
        games = self._builder.build_games_from_configs(
            matched_configs,
            installed,
        )
        logger.info(
            "[UbisoftLibrary] local binary library: %d games (from %d matched configs)",
            len(games),
            len(matched_configs),
        )
        return games

    @staticmethod
    def _import_ubisoft_parser() -> (
        tuple[ParseConfigurationsFn, ParseOwnershipFn] | None
    ):
        """Try to import the parser functions at runtime.

        Returns:
            ``(parse_configurations, parse_ownership)`` tuple, or
            ``None`` if the parser module isn't importable.
        """
        try:
            from ..parser import (
                parse_configurations,
                parse_ownership,
            )
        except ImportError as e:
            logger.error(
                "[UbisoftLibrary] ubisoft_parser unavailable: %s",
                e,
            )
            return None
        return parse_configurations, parse_ownership
