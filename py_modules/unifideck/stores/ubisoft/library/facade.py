"""
Ubisoft library facade — orchestrates fetch, detect, build, filter.

OP-57a | py_modules/unifideck/stores/ubisoft/library/facade.py

``UbisoftLibrary`` is the public entry-point of the library sub-package.
It composes the work of:

* ``fetch.py`` (OP-57b) — pull the UPC owned-games catalog;
* ``data_loader.py`` (OP-57c) — load installed-state from disk markers;
* ``detection.py`` (OP-57f) — detect installs the catalog doesn't know about;
* ``manifest.py`` (OP-57e) — produce display-ready ``GameRecord`` entries;
* ``steam_filter.py`` (OP-55i) — hide games already on Steam.

The result is the merged list of owned + installed Ubisoft games that
the UI displays. Cached in-memory by the store and invalidated on:
auth state change, install/uninstall, manual user refresh.
"""

from __future__ import annotations
import logging
import os
from collections.abc import Callable
from typing import Any
from ....core.types import Game
from ..config import UbisoftConfig
from ..id_map import UbisoftIdMap
from ..paths import UbisoftPrefixPaths
from .detection import _InstallDetector
from .fetch import _LibraryFetcher
from .manifest import _VisibleManifestProcessor

logger = logging.getLogger(__name__)


class UbisoftLibrary:
    """Ubisoft Connect library facade — exposes the user's owned/installed games.

    Coordinates three specialists: an install detector
    (filesystem + registry probes), a library fetcher (online
    user library), and the visible-manifest processor that
    merges them into the public game list.
    """

    def __init__(
        self,
        config: UbisoftConfig,
        paths: UbisoftPrefixPaths,
        id_map: UbisoftIdMap,
        queue_template_creation: Callable[[], None],
    ) -> None:
        """Wire dependencies and build the library specialists.

        Args:
            config: Ubisoft store config.
            paths: Ubisoft prefix paths.
            id_map: Ubisoft ID translation map (space_id ↔ install_id ↔
                launch_id).
            queue_template_creation: Callback enqueueing a template
                prefix rebuild when the library needs one.
        """
        self._config = config
        self._paths = paths
        self._id_map = id_map
        self._queue_template_creation = queue_template_creation
        self._detector = _InstallDetector(
            config=config,
            id_map=id_map,
        )
        self._fetcher = _LibraryFetcher(
            config=config,
            paths=paths,
            id_map=id_map,
        )
        self._manifest = _VisibleManifestProcessor(
            config=config,
            id_map=id_map,
            load_json_file_safe=self._detector.load_json_file_safe,
        )

    async def get_library(self) -> list[Game]:
        """Build the merged owned + installed library list for the UI.

        Pipeline: detect installed games on disk → fetch the owned
        list from UPC binaries → optionally override visibility via
        the manifest → schedule a background template creation when
        the bootstrap marker is missing.

        Returns:
            List of ``Game`` records. Empty list on any error
            (logged and swallowed).
        """
        try:
            installed = await self._detector.get_installed()
            local_games = await self._fetcher.fetch_local_binaries(
                installed,
            )
            if local_games is None:
                logger.info(
                    "[UbisoftLibrary] no local binary data available yet",
                )
                return []
            logger.info(
                "[UbisoftLibrary] library: %d games from local binaries",
                len(local_games),
            )
            override_manifest = self._manifest.load_manifest()
            if override_manifest:
                local_games = self._manifest.apply_filter(
                    local_games,
                    installed,
                    override_manifest,
                    source_label="override",
                )
            if local_games:
                template_dir = self._config.template_dir_expanded
                template_marker = os.path.join(
                    template_dir,
                    self._config.bootstrap_marker,
                )
                if not os.path.isfile(template_marker):
                    self._queue_template_creation()
            return local_games
        except Exception as e:
            logger.exception(
                "[UbisoftLibrary] error fetching library: %s",
                e,
            )
            return []

    async def get_installed(self) -> dict[str, Any]:
        """Return the dict of installed Ubisoft games.

        Delegates to the install detector which walks every per-game
        prefix that bears the bootstrap marker.

        Returns:
            ``{space_id: install_info}`` for every detected install.
        """
        return await self._detector.get_installed()

    def get_installed_game_info(
        self,
        game_id: str,
    ) -> dict[str, Any] | None:
        """Return install info for one Ubisoft game (if installed).

        Args:
            game_id: Ubisoft space_id.

        Returns:
            Install info dict, or ``None`` if the game isn't installed.
        """
        return self._detector.get_installed_game_info(game_id)

    def find_game_executable(
        self,
        install_path: str,
    ) -> str | None:
        """Locate the game executable inside an install directory.

        Args:
            install_path: Absolute path to the install directory.

        Returns:
            Path to the executable, or ``None`` if no candidate
            could be identified.
        """
        return self._detector.find_game_executable(install_path)

    async def write_install_marker(
        self,
        space_id: str,
        install_path: str,
        executable: str,
        game_title: str = "",
    ) -> None:
        """Persist an install marker so the game appears without rescan.

        Args:
            space_id: Ubisoft space_id.
            install_path: Windows-style install path inside the prefix.
            executable: Game executable name.
            game_title: Display title (used for SteamGridDB lookups).
        """
        await self._detector.write_install_marker(
            space_id=space_id,
            install_path=install_path,
            executable=executable,
            game_title=game_title,
        )

    @staticmethod
    def get_game_official_url(game_id: str) -> str:
        """Return the canonical Ubisoft store URL for one game.

        Args:
            game_id: Ubisoft space_id.

        Returns:
            Store URL string.
        """
        return _InstallDetector.get_game_official_url(game_id)
