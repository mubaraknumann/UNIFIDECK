"""Steam-owned title detection with mtime-fingerprint caching.

OP-14b | py_modules/unifideck/steam/owned_games.py

Walks the user's Steam install to enumerate every
installed Steam game's title. Used by cross-store
deduplication to drop entries the user already owns
natively (no point showing the same game twice in the
unified library).

Pipeline:

1. Find the Steam install (``find_steam_path``);
2. Compute a fingerprint of the install state
   (mtime of ``libraryfolders.vdf`` + mtime of each
   ``steamapps/`` directory);
3. If the cached fingerprint matches, return cached
   results;
4. Otherwise: walk every library root, every
   ``appmanifest_*.acf``, extract the ``name`` field
   via regex, normalise the title.

The fingerprint-based cache means the dedup logic
doesn't re-walk the filesystem on every sync — only
when the user installs/uninstalls something.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING
from ..metadata.unifidb import normalize_title_for_matching
from .library import find_steam_path

if TYPE_CHECKING:
    from ..config import ConfigManager

logger = logging.getLogger(__name__)

_ACF_NAME_PATTERN = re.compile(r'"name"\s+"([^"]*)"')
_LIBFOLDER_PATH_PATTERN = re.compile(r'"path"\s+"([^"]*)"')
_Fingerprint = tuple[str, float | None, tuple[tuple[str, float | None], ...]]
_cache: tuple[_Fingerprint, frozenset[str]] | None = None


def get_owned_titles(config: ConfigManager | None = None) -> frozenset[str]:
    """Return the set of normalised Steam-owned titles, with caching.

    Returns an empty frozenset on missing Steam install
    (no error — Steam may legitimately not be present
    in dev contexts).

    The first call walks the filesystem; subsequent
    calls return the cached set as long as the
    fingerprint matches. Result is a frozenset for safe
    sharing — callers can iterate / membership-test
    without worrying about mutation.

    Args:
        config: optional ``ConfigManager``.

    Returns:
        Frozenset of normalised title strings.
    """
    global _cache
    steam_path = find_steam_path(config)
    if steam_path is None:
        logger.debug("[owned_games] no Steam install found")
        return frozenset()
    fingerprint = _compute_fingerprint(Path(steam_path))
    if _cache is not None and _cache[0] == fingerprint:
        return _cache[1]
    titles = _scan_all_libraries(Path(steam_path))
    logger.info(
        "[owned_games] indexed %d Steam-native title(s) from %s",
        len(titles),
        steam_path,
    )
    _cache = (fingerprint, titles)
    return titles


def invalidate_cache() -> None:
    """Force the next ``get_owned_titles`` to re-walk the filesystem.

    Used by tests and by admin actions that know
    something changed but want to avoid waiting for
    the fingerprint to catch up.
    """
    global _cache
    _cache = None


def _compute_fingerprint(steam_path: Path) -> _Fingerprint:
    """Return a tuple summarising the install state for cache invalidation.

    Three components:

    * Steam path itself;
    * mtime of ``libraryfolders.vdf`` (changes when
      the user adds/removes a library);
    * Per-library: ``(path, steamapps_mtime)``
      tuples (mtime changes when manifests are added
      or removed).

    Comparing fingerprints by equality is the
    invalidation check — same tuple means same state.

    Args:
        steam_path: Steam install root.

    Returns:
        Hashable fingerprint tuple.
    """
    libfolders_vdf = steam_path / "steamapps" / "libraryfolders.vdf"
    libfolders_mtime = _stat_mtime(libfolders_vdf)
    library_roots = _list_library_roots(steam_path)
    per_library: list[tuple[str, float | None]] = []
    for root in library_roots:
        steamapps = root / "steamapps"
        per_library.append((str(root), _stat_mtime(steamapps)))
    return (str(steam_path), libfolders_mtime, tuple(per_library))


def _stat_mtime(path: Path) -> float | None:
    """Return ``path``'s mtime, or ``None`` if it doesn't exist / can't stat.

    ``None`` is treated as a valid fingerprint value
    (different from any real mtime) so missing paths
    are stable invalidation keys.

    Args:
        path: filesystem path.

    Returns:
        mtime float or ``None``.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _scan_all_libraries(steam_path: Path) -> frozenset[str]:
    """Walk every library root, harvest titles, return as frozenset.

    Per-library OSError is logged at WARN but doesn't
    abort the scan (partial result is better than no
    result; one inaccessible library shouldn't make
    dedup useless).

    Args:
        steam_path: Steam install root.

    Returns:
        Frozenset of normalised titles.
    """
    titles: set[str] = set()
    for library_root in _list_library_roots(steam_path):
        try:
            titles.update(_titles_from_library(library_root))
        except OSError as e:
            logger.warning(
                "[owned_games] could not scan %s: %s",
                library_root,
                e,
            )
    return frozenset(titles)


def _list_library_roots(steam_path: Path) -> list[Path]:
    """Read ``libraryfolders.vdf`` to enumerate every Steam library root.

    Always includes the main Steam install root as
    the first entry. Then parses
    ``libraryfolders.vdf`` for additional library
    roots (e.g. SD card, secondary drive). Skips
    duplicates of the primary path and entries whose
    ``steamapps/`` doesn't exist (missing drive).

    Args:
        steam_path: primary Steam install.

    Returns:
        Ordered list of library roots.
    """
    roots: list[Path] = [steam_path]
    libfolders_vdf = steam_path / "steamapps" / "libraryfolders.vdf"
    if not libfolders_vdf.is_file():
        return roots
    try:
        content = libfolders_vdf.read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        logger.warning(
            "[owned_games] cannot read %s: %s",
            libfolders_vdf,
            e,
        )
        return roots
    for match in _LIBFOLDER_PATH_PATTERN.finditer(content):
        candidate = Path(match.group(1))
        if candidate == steam_path:
            continue
        if (candidate / "steamapps").is_dir():
            roots.append(candidate)
    return roots


def _titles_from_library(library_root: Path) -> set[str]:
    """Glob every ``appmanifest_*.acf`` and harvest the normalised title.

    Skips libraries without a ``steamapps/`` dir
    silently. Empty / whitespace-only normalised
    titles are dropped — they don't help dedup.

    Args:
        library_root: one Steam library root.

    Returns:
        Set of normalised titles.
    """
    steamapps = library_root / "steamapps"
    if not steamapps.is_dir():
        return set()
    titles: set[str] = set()
    for manifest in steamapps.glob("appmanifest_*.acf"):
        title = _extract_name_from_manifest(manifest)
        if title:
            normalized = normalize_title_for_matching(title)
            if normalized:
                titles.add(normalized)
    return titles


def _extract_name_from_manifest(manifest: Path) -> str | None:
    """Pull the ``name`` field from an ``.acf`` manifest via regex.

    Steam's ``.acf`` files are VDF (text format).
    Full VDF parsing would be overkill; the regex
    targets just the ``"name" "..."`` line we need.

    Read errors are tolerated and logged — one
    unreadable manifest shouldn't break the whole
    library scan.

    Args:
        manifest: path to one ``appmanifest_*.acf``.

    Returns:
        Title string, or ``None`` if not found / can't
        read.
    """
    try:
        content = manifest.read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        logger.warning(
            "[owned_games] cannot read %s: %s",
            manifest,
            e,
        )
        return None
    match = _ACF_NAME_PATTERN.search(content)
    return match.group(1) if match else None
