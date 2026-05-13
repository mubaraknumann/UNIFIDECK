"""Steam-side helpers — install paths, store search, artwork, shortcuts.

OP-14 | py_modules/unifideck/steam/__init__.py

Helper functions that interact with the local Steam
installation:

* ``library``      — locate the Steam install, search
  the Steam store API by title;
* ``owned_games``  — read the owned titles from a
  local Steam userdata file (used by cross-store
  dedup);
* ``shortcuts``    — read / write Steam's
  ``shortcuts.vdf`` for non-Steam games;
* ``steamgriddb``  — fetch artwork from steamgriddb.com.

The top-level ``__init__`` re-exports the most-used
names; the optional ``steam_utils`` import handles the
legacy module that may not be present in all checkouts.
"""

from .library import find_steam_path, search_store
from .steamgriddb import SteamGridDBClient, fetch_all_kinds, search_artwork

try:
    from .steam_utils import (  # noqa: F401
        get_logged_in_steam_user,
        migrate_user0_to_logged_in_user,
    )
except ImportError:
    pass

__all__ = [
    "SteamGridDBClient",
    "fetch_all_kinds",
    "find_steam_path",
    "search_artwork",
    "search_store",
]
