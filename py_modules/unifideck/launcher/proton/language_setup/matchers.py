"""Locale matching helpers — best-effort fuzzy match between BCP-47 tags and per-store language strings."""

from __future__ import annotations
from .resolver import LOCALE_MAP
def smart_match_locale(
    target: str,
) -> tuple[str, str, str, str] | None:
    """Best-effort match of a BCP-47 tag against the ``LOCALE_MAP`` keys.

    Tries an exact match first, then a base-language match
    (``fr-CA`` → matches the first ``fr-*`` entry).

    Args:
        target: BCP-47 tag.

    Returns:
        Tuple ``(lcid, slanguage, locale_name, scountry)``,
        or ``None`` if no match.
    """
    if not target:
        return None
    if target in LOCALE_MAP:
        return LOCALE_MAP[target]
    base = target.split("-", maxsplit=1)[0].lower()
    for code, data in LOCALE_MAP.items():
        if code.split("-")[0].lower() == base:
            return data
    return None
def smart_match_gog_language(
    target: str, available: list[str],
) -> str | None:
    """Best-effort match of a BCP-47 tag against GOG's installed-languages list.

    Tries an exact match first, then a base-language match.

    Args:
        target: BCP-47 tag.
        available: Languages GOG advertises as installed.

    Returns:
        Matching entry from ``available``, or ``None`` if
        no acceptable match.
    """
    if not target or not available:
        return None
    if target in available:
        return target
    base = target.split("-", maxsplit=1)[0].lower()
    for lang in available:
        if lang.split("-")[0].lower() == base:
            return lang
    return None