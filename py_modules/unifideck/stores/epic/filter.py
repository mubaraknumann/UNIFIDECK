"""filter.py — Drop UE assets, plugins, mods, and mobile-only entries.

# OP-48f | py_modules/unifideck/stores/epic/filter.py | Depends: OP-48c

Mirrors Heroic Games Launcher's library filter. Without it the user
sees thousands of free UE Marketplace assets they don't own as games.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)
ASSET_CATEGORIES: set[str] = {'assets', 'asset-format', 'plugins', 'projects'}
MOBILE_PLATFORMS: set[str] = {'Android', 'iOS'}


def has_ue_namespace(metadata: dict[str, Any]) -> bool:
    """Check whether the metadata's ``namespace`` field is ``"ue"``.

    Args:
        metadata: Game-metadata dict from legendary.

    Returns:
        True iff the entry is from the UE Marketplace namespace.
    """
    return metadata.get('namespace') == 'ue'


def has_asset_category(metadata: dict[str, Any]) -> bool:
    """Check whether the metadata declares an asset/plugin/project category.

    Args:
        metadata: Game-metadata dict from legendary.

    Returns:
        True iff any category path is in ``ASSET_CATEGORIES``.
    """
    categories = metadata.get('categories') or []
    for cat in categories:
        if isinstance(cat, dict) and cat.get('path') in ASSET_CATEGORIES:
            return True
    return False


def has_mod_category(metadata: dict[str, Any]) -> bool:
    """Check whether the metadata declares a ``mods`` category.

    Args:
        metadata: Game-metadata dict from legendary.

    Returns:
        True iff the entry is tagged as a mod.
    """
    categories = metadata.get('categories') or []
    for cat in categories:
        if isinstance(cat, dict) and cat.get('path') == 'mods':
            return True
    return False


def is_mobile_only(metadata: dict[str, Any]) -> bool:
    """Check whether the entry is published only on mobile platforms.

    Returns False if any release-info entry covers a non-mobile
    platform.

    Args:
        metadata: Game-metadata dict from legendary.

    Returns:
        True iff every ``releaseInfo`` entry targets Android/iOS only.
    """
    release_info = metadata.get('releaseInfo') or []
    if not release_info:
        return False
    for info in release_info:
        platforms = info.get('platform') if isinstance(info, dict) else None
        if not platforms:
            return False
        if not all(p in MOBILE_PLATFORMS for p in platforms):
            return False
    return True


def should_filter_epic_item(game_data: dict[str, Any]) -> bool:
    """Decide whether to drop one Epic library entry.

    Filters out UE namespace items, assets/plugins/projects,
    mods, and mobile-only releases. Mirrors Heroic Games
    Launcher's library filter.

    Args:
        game_data: One entry from ``legendary list --json``.

    Returns:
        True iff the entry should be hidden from the library.
    """
    metadata = game_data.get('metadata') or {}
    if not isinstance(metadata, dict):
        return False
    if has_ue_namespace(metadata):
        return True
    if has_asset_category(metadata):
        return True
    if has_mod_category(metadata):
        return True
    if is_mobile_only(metadata):
        return True
    return False
