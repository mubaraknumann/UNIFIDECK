"""GOG-store language setup — patches per-game goggame-*.info files with the user's preferred language."""

from __future__ import annotations
import glob
import json
import logging
import os
from typing import TYPE_CHECKING
from .matchers import smart_match_gog_language
from .registry_io import _atomic_write_text
from .resolver import GOG_DISPLAY_NAMES, get_unifideck_language
if TYPE_CHECKING:
    from ....config import ConfigManager
logger = logging.getLogger(__name__)
def _find_goggame_info(game_id: str, install_dir: str) -> str | None:
    """Walk up to 4 directory levels looking for ``goggame-<id>.info``.

    First tries the exact filename, then falls back to a glob
    of ``goggame-*.info`` files that start with the requested
    id (covers DLC/variants).

    Args:
        game_id: GOG game identifier.
        install_dir: Game install directory.

    Returns:
        Path string to the matching info file, or ``None``
        if nothing was found.
    """
    search_dir = install_dir
    for _ in range(4):
        candidate = os.path.join(search_dir, f"goggame-{game_id}.info")
        if os.path.exists(candidate):
            return candidate
        candidates = glob.glob(
            os.path.join(search_dir, "goggame-*.info"),
        )
        matching = [
            c for c in candidates
            if os.path.basename(c).startswith(f"goggame-{game_id}")
        ]
        if matching:
            return matching[0]
        parent = os.path.dirname(search_dir)
        if parent == search_dir:
            break
        search_dir = parent
    return None

def apply_gog_language(
    game_id: str, install_dir: str, config: ConfigManager | None = None,
) -> bool:

    """Patch the per-game goggame-*.info to the user's language.

    Resolves the preferred language from config, finds the
    info file, matches against the languages GOG declares
    available, and atomically rewrites the file with the
    single-language list + display name.

    Args:
        game_id: GOG game identifier.
        install_dir: Game install directory.
        config: ConfigManager.

    Returns:
        True iff the info file was patched.
    """
    language = get_unifideck_language(config)
    logger.info(
        "[language_setup.gog] applying %s to game=%s dir=%s",
        language, game_id, install_dir,
    )
    info_path = _find_goggame_info(game_id, install_dir)
    if info_path is None:
        logger.info(
            "[language_setup.gog] no goggame-%s.info found (searched "
            "4 levels up from %s), skipping", game_id, install_dir,
        )
        return False
    try:
        with open(info_path, encoding="utf-8") as fh:
            info = json.load(fh)
    except (OSError, json.JSONDecodeError) as err:
        logger.warning(
            "[language_setup.gog] read %s failed: %s", info_path, err,
        )
        return False
    installed = info.get("languages", [])
    matched = smart_match_gog_language(language, installed)
    if matched is None:
        matched = language
    display_name = GOG_DISPLAY_NAMES.get(matched, matched)
    info["language"] = display_name
    info["languages"] = [matched]
    try:
        _atomic_write_text(info_path, json.dumps(info, indent=2))
    except OSError as err:
        logger.warning(
            "[language_setup.gog] write %s failed: %s", info_path, err,
        )
        return False
    logger.info(
        "[language_setup.gog] set %s → %s (%s)",
        info_path, matched, display_name,
    )
    return True