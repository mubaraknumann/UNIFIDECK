"""Utility helpers — re-exports of path + locale + config helpers.

OP-21 | py_modules/unifideck/utils/__init__.py

Convenience surface for the path-resolution helpers used
in many places (game-directory roots, default install
locations, games-map JSON path). Locale and config-reading
helpers live alongside but aren't re-exported here — they
have their own import path.
"""

from .paths import (
    DEFAULT_GAMES_MAP,
    DEFAULT_INSTALL_DIRS,
    DEFAULT_PATHS,
    DEFAULT_SD_ROOT,
    GAMES_MAP_PATH,
    dedupe_paths,
    ensure_games_map_dir,
    expand,
    get_all_game_directories,
    get_games_map_path,
)

__all__ = [
    "DEFAULT_GAMES_MAP",
    "DEFAULT_INSTALL_DIRS",
    "DEFAULT_PATHS",
    "DEFAULT_SD_ROOT",
    "GAMES_MAP_PATH",
    "dedupe_paths",
    "ensure_games_map_dir",
    "expand",
    "get_all_game_directories",
    "get_games_map_path",
]
