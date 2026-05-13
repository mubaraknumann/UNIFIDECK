"""Compatibility — ProtonDB / Deck Verified ratings + Proton helpers.

OP-12 | py_modules/unifideck/compatibility/__init__.py

Two complementary surfaces:

* **Rating fetchers** (``library``) — query ProtonDB +
  Steam's Deck Verified status for a game title, cache
  the results, expose background prefetch for the whole
  library.
* **Proton helpers** (``proton_helpers``) — read /
  write the Steam compat-tool override for an AppID,
  resolve the actual Proton path on disk, temporarily
  clear and restore the override (used during certain
  launch flows).

Re-exports the public names from both submodules so
callers can ``from unifideck.compatibility import
CompatLibrary, ProtonToolsManager`` without knowing the
split.
"""

from .library import (
    BackgroundCompatFetcher,
    CompatLibrary,
    CompatRating,
    fetch_deck_verified,
    fetch_protondb_rating,
    get_compat_for_title,
    load_compat_cache,
    prefetch_compat,
    save_compat_cache,
    search_steam_store,
)
from .proton_helpers import (
    CompatToolResult,
    ProtonToolsManager,
    get_compat_tool_for_app,
    get_compat_tool_for_game,
    get_saved_proton_tool,
    is_linux_runtime,
    resolve_proton_path,
    restore_compat_tool,
    save_proton_setting,
    temporarily_clear_compat_tool,
)

__all__ = [
    "BackgroundCompatFetcher",
    "CompatLibrary",
    "CompatRating",
    "CompatToolResult",
    "ProtonToolsManager",
    "fetch_deck_verified",
    "fetch_protondb_rating",
    "get_compat_for_title",
    "get_compat_tool_for_app",
    "get_compat_tool_for_game",
    "get_saved_proton_tool",
    "is_linux_runtime",
    "load_compat_cache",
    "prefetch_compat",
    "resolve_proton_path",
    "restore_compat_tool",
    "save_compat_cache",
    "save_proton_setting",
    "search_steam_store",
    "temporarily_clear_compat_tool",
]
