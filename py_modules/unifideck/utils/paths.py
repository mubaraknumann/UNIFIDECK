"""Game-install path resolution + mount-root scanning.

OP-21c | py_modules/unifideck/utils/paths.py

Resolves the set of directories where installed games may
live. The plugin needs this for the discovery pass (find
games that were installed outside Unifideck) and for the
default install location per store.

Three layers of sources:

* **Per-store defaults** (``DEFAULT_INSTALL_DIRS``) —
  hard-coded conventional paths;
* **Config overrides** — ``stores.<id>.install_dir`` per
  store + ``download.custom_path`` for user-picked extra
  location;
* **Removable media scan** — walks ``/run/media`` (Steam
  Deck's microSD mount point) looking for game roots two
  levels deep.

All paths go through ``expand`` to handle ``~`` and
``$VAR`` substitution, and through ``dedupe_paths`` to
ensure the discovery walk doesn't process the same
directory twice (via different symlinks).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config_helpers import get_cfg

if TYPE_CHECKING:
    from ..config import ConfigManager

logger = logging.getLogger(__name__)

DEFAULT_INSTALL_DIRS = {
    "epic": "~/Games/Epic",
    "gog": "~/GOG Games",
    "amazon": "~/Games/Amazon",
    "microsoft": "~/Games/Microsoft",
    "ubisoft": "~/Games/Ubisoft",
}

DEFAULT_GAMES_MAP = "~/.local/share/unifideck/games.map"
DEFAULT_SD_ROOT = "/run/media"

GAMES_MAP_PATH = str(Path(DEFAULT_GAMES_MAP).expanduser())
DEFAULT_PATHS = {
    store: str(Path(path).expanduser()) for store, path in DEFAULT_INSTALL_DIRS.items()
}


def expand(path: str) -> str:
    """Apply ``$VAR`` then ``~`` expansion to a path string.

    Order matters: ``expandvars`` first (so ``$HOME``
    becomes ``/home/user``) then ``expanduser`` (so a
    bare ``~`` becomes the same). The double pass handles
    paths like ``$HOME/Games`` and ``~/Games`` uniformly.

    Args:
        path: raw path with optional ``$VAR`` and ``~``.

    Returns:
        Expanded string path.
    """
    return str(Path(os.path.expandvars(path)).expanduser())


def dedupe_paths(paths: list[str]) -> list[str]:
    """Drop duplicate paths preserving order, comparing by ``normpath``.

    Two entries are duplicates if their ``os.normpath``
    is identical — handles trailing slashes, doubled
    separators, ``.``/``..`` segments. The first
    occurrence wins; original casing is preserved
    (normpath only normalises separator / segment
    structure, not case).

    Args:
        paths: list of path strings.

    Returns:
        Deduplicated list preserving input order.
    """
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        norm = os.path.normpath(p)
        if norm in seen:
            continue
        seen.add(norm)
        out.append(p)
    return out


def _cfg(config: ConfigManager | None, key: str, default: Any) -> Any:
    """Thin wrapper around ``get_cfg`` — local convention.

    Same idea as the ``_cfg`` in ``core/manifest.py``: a
    short alias keeps call sites compact.

    Args:
        config: optional ``ConfigManager``.
        key: dotted config key.
        default: fallback.

    Returns:
        Config value or default.
    """
    return get_cfg(config, key, default)


def get_all_game_directories(config: ConfigManager | None = None) -> list[str]:
    """Resolve every directory the discovery pass should walk.

    Combines three sources:

    1. Per-store install dirs (config or default);
    2. The user's custom path (``download.custom_path``)
       if set;
    3. Game roots found by ``_scan_mount_root`` on the
       SD-card mount root (``/run/media`` by default).

    Output is filtered to existing directories (skipping
    typos / missing SD cards) and deduplicated. The
    discovery pass calls this once at boot.

    Args:
        config: optional ``ConfigManager``.

    Returns:
        Deduplicated list of existing directory paths.
    """
    candidates: list[str] = []
    for store, default in DEFAULT_INSTALL_DIRS.items():
        path = _cfg(config, f"stores.{store}.install_dir", default)
        candidates.append(expand(path))
    custom = get_cfg(config, "download.custom_path", "")
    if custom:
        candidates.append(expand(custom))
    media_root = get_cfg(config, "paths.sd_card_root", DEFAULT_SD_ROOT)
    candidates.extend(_scan_mount_root(media_root))
    existing = [p for p in candidates if Path(p).is_dir()]
    return dedupe_paths(existing)


def _collect_game_dirs(parent_path: Path) -> list[str]:
    """Return the conventional game-dir subpaths under ``parent_path``.

    Two well-known subdirectory names: ``"Games"`` and
    ``"GOG Games"`` (the latter is GOG Galaxy's
    default). Symlinks are skipped to avoid duplicates
    when the user has a symlink farm.

    Args:
        parent_path: directory to inspect.

    Returns:
        List of matching subpaths (typically 0-2).
    """
    found: list[str] = []
    for sub in ("Games", "GOG Games"):
        p = parent_path / sub
        if p.is_dir() and not p.is_symlink():
            found.append(str(p))
    return found


def _scan_level2(level1_path: Path) -> list[str]:
    """Walk one level deeper to find nested game roots.

    Used inside ``_scan_mount_root`` to handle the
    nested case ``/run/media/<user>/<drive>/Games``:
    level1 is ``<user>``, level2 is ``<drive>``.
    Each level2 subdirectory is checked for the
    standard game-dir subpaths.

    OSError on iteration is swallowed silently — happens
    when a mount disappeared between listing levels.

    Args:
        level1_path: a ``<user>``-level directory.

    Returns:
        List of game-root paths found at level 2.
    """
    found: list[str] = []
    try:
        for level2_path in level1_path.iterdir():
            if not level2_path.is_dir() or level2_path.is_symlink():
                continue
            found.extend(_collect_game_dirs(level2_path))
    except OSError:
        pass
    return found


def _scan_mount_root(root: str) -> list[str]:
    """Walk the mount root looking for game directories at depth 1 and 2.

    The Steam Deck's removable-media convention is
    ``/run/media/<user>/<volume>``. Some users have the
    games at level 1 (root → volume → Games) and others
    at level 2 (root → user → volume → Games); this
    function scans both.

    Symlinks skipped at every level. OSError on root
    iteration is logged at DEBUG and the partial result
    is returned.

    Args:
        root: mount-root path (e.g. ``"/run/media"``).

    Returns:
        List of discovered game directory paths.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        return []
    found: list[str] = []
    try:
        for level1_path in root_path.iterdir():
            if not level1_path.is_dir() or level1_path.is_symlink():
                continue
            found.extend(_collect_game_dirs(level1_path))
            found.extend(_scan_level2(level1_path))
    except OSError as e:
        logger.debug(
            "[paths] mount scan failed on %s: %s",
            root,
            e,
        )
    return found


def get_games_map_path(config: ConfigManager | None = None) -> str:
    """Return the resolved path of the unified ``games.map`` file.

    Config-overridable via ``paths.games_map``;
    defaults to ``~/.local/share/unifideck/games.map``.

    Args:
        config: optional ``ConfigManager``.

    Returns:
        Expanded absolute path string.
    """
    raw = get_cfg(config, "paths.games_map", DEFAULT_GAMES_MAP)
    return expand(raw)


def ensure_games_map_dir(config: ConfigManager | None = None) -> str | None:
    """Create the parent directory of ``games.map`` if it doesn't exist.

    Idempotent (``exist_ok=True``). Returns the directory
    path on success or ``None`` on OSError (logged at
    WARN — typically permission denied on locked-down
    setups).

    Args:
        config: optional ``ConfigManager``.

    Returns:
        Created/existing directory path, or ``None`` on
        failure.
    """
    path = Path(get_games_map_path(config))
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        return str(parent)
    except OSError as e:
        logger.warning(
            "[paths] mkdir %s failed: %s",
            parent,
            e,
        )
        return None
