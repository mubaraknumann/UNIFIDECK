"""Metacritic score lookup — slug generation + composer-API parsing.

OP-20b | py_modules/unifideck/metadata/metacritic.py

Fetches Metacritic scores for a game by trying multiple
slug candidates generated from the title. Metacritic's
URL slug isn't deterministic from the title — they vary
edition suffixes, numeral styles (``"3"`` vs ``"III"``),
and trademark handling. The strategy is to enumerate
candidate slugs and try each until one returns data.

Candidate generation:

1. Raw title;
2. Title with ™/® stripped (``clean_title``);
3. Title with edition suffixes stripped
   (``strip_suffixes``);
4. Numeral variants of each of the above
   (``get_numeral_variants`` — generates Arabic↔Roman
   conversions);
5. Each candidate slugified
   (``slugify_game_name``).

The first slug that returns a parseable composer-API
response wins; ``MetacriticScore`` carries metascore +
user score + sanitized description.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from ..utils.config_helpers import get_cfg

if TYPE_CHECKING:
    from ..config import ConfigManager

logger = logging.getLogger(__name__)

METACRITIC_COMPOSER_URL = (
    "https://backend.metacritic.com/composer/metacritic/pages/"
    "games-critic-reviews/{slug}/platform/pc/web"
)
DEFAULT_FETCH_TIMEOUT = 10

_EDITION_SUFFIXES = [
    r":?\s*Director's Cut",
    r":?\s*Game of the Year Edition",
    r":?\s*GOTY Edition",
    r":?\s*Remastered",
    r":?\s*Definitive Edition",
    r":?\s*Bonus Edition",
    r":?\s*Deluxe Edition",
    r":?\s*Special Edition",
    r":?\s*Anniversary Edition",
    r":?\s*Complete Edition",
    r":?\s*Ultimate Edition",
    r":?\s*Gold Edition",
    r":?\s*Enhanced Edition",
]

_ROMAN_MAP = {"1": "I", "2": "II", "3": "III", "4": "IV", "5": "V"}
_ARABIC_MAP = {v: k for k, v in _ROMAN_MAP.items()}


def slugify_game_name(name: str) -> str:
    """Convert a game name to a Metacritic-style URL slug.

    Pipeline:

    1. NFKD normalise + drop non-ASCII (handles
       accented characters);
    2. Lower-case;
    3. Replace ``"+"`` with ``"-plus-"`` (Metacritic
       quirk);
    4. Drop everything not alphanumeric / space /
       hyphen;
    5. Collapse spaces and runs of hyphens to single
       hyphens;
    6. Strip leading/trailing hyphens.

    Args:
        name: human-readable title.

    Returns:
        URL-safe slug.
    """
    name = (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode("utf-8")
        .lower()
    )
    name = name.replace("+", "-plus-")
    name = re.sub(r"[^a-z0-9 \-]+", "", name)
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"-+", "-", name)
    return name.strip("-")


def clean_title(title: str) -> str:
    """Strip ™ and ® unicode characters from a title.

    Args:
        title: raw title.

    Returns:
        Trademark-free copy.
    """
    return re.sub(r"[\u2122\u00AE]", "", title).strip()


def strip_suffixes(title: str) -> str:
    """Remove common edition suffixes from ``title``.

    Walks ``_EDITION_SUFFIXES`` and applies each regex
    (case-insensitive). Stripping is greedy — multiple
    suffixes can be removed if all match (rare).

    Args:
        title: raw title.

    Returns:
        Title with edition suffixes stripped.
    """
    cleaned = title
    for suffix in _EDITION_SUFFIXES:
        cleaned = re.sub(suffix, "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def to_roman(num_str: str) -> str | None:
    """Convert ``"1".."5"`` to ``"I".."V"`` or ``None`` if out of range.

    Args:
        num_str: digit string.

    Returns:
        Roman numeral or ``None``.
    """
    return _ROMAN_MAP.get(num_str)


def to_arabic(roman_str: str) -> str | None:
    """Convert ``"I".."V"`` to ``"1".."5"`` or ``None``.

    Args:
        roman_str: Roman numeral.

    Returns:
        Arabic digit string or ``None``.
    """
    return _ARABIC_MAP.get(roman_str)


def get_numeral_variants(title: str) -> list[str]:
    """Generate Arabic↔Roman variants of ``title`` (end-position + word-internal).

    Two passes:

    1. **Trailing-numeral swap** — ``"Halo 3"`` →
       ``"Halo III"`` and vice versa. Handled by regex
       anchored at end-of-string.
    2. **Whole-word swap** — replace every word-boundary
       Arabic with Roman (and vice versa). Captures
       middle-of-title occurrences like
       ``"Saints Row 2 Remastered"``.

    Dedupes via ``dict.fromkeys`` (preserves order).

    Args:
        title: raw title.

    Returns:
        List of unique variant titles (excluding the
        input itself).
    """
    candidates: list[str] = []
    m = re.search(r"\b(I|II|III|IV|V)$", title)
    if m:
        arabic = to_arabic(m.group(1))
        if arabic:
            candidates.append(title[: m.start()] + arabic)
    m = re.search(r"\b([1-5])$", title)
    if m:
        roman = to_roman(m.group(1))
        if roman:
            candidates.append(title[: m.start()] + roman)

    def _arabic_to_roman(match: re.Match) -> str:
        """Map an Arabic match to its Roman equivalent (or unchanged).

        Inner helper for the ``re.sub``
        pass that swaps standalone
        Arabic numerals (1–5) embedded
        in titles to their Roman form.
        Returns the original group on
        miss so unrecognised digits
        pass through untouched.

        Args:
            match: regex match.

        Returns:
            Substitution string.
        """
        return to_roman(match.group(1)) or match.group(0)

    def _roman_to_arabic(match: re.Match) -> str:
        """Map a Roman match to its Arabic equivalent (or unchanged).

        Inverse of ``_arabic_to_roman`` —
        used in the parallel sub pass
        that generates the Arabic
        candidate forms from
        Roman-numeral titles.

        Args:
            match: regex match.

        Returns:
            Substitution string.
        """
        return to_arabic(match.group(1)) or match.group(0)

    subbed = re.sub(r"\b([1-5])\b", _arabic_to_roman, title)
    if subbed != title:
        candidates.append(subbed)
    subbed = re.sub(r"\b(I|II|III|IV|V)\b", _roman_to_arabic, title)
    if subbed != title:
        candidates.append(subbed)
    return list(dict.fromkeys(candidates))


def sanitize_description(text: str, max_length: int = 1000) -> str:
    """Collapse whitespace and truncate ``text`` to ``max_length`` chars.

    Used on Metacritic's prose descriptions which can
    be multi-paragraph and very long. The truncation
    keeps a clean ellipsis (``…``) appended after a
    right-strip so we don't end on whitespace.

    Args:
        text: raw description.
        max_length: max char length (default 1000).

    Returns:
        Sanitized text.
    """
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_length:
        return text[: max_length - 1].rstrip() + "…"
    return text


@dataclass
class MetacriticScore:
    """Typed shape for a successful Metacritic lookup.

    Attributes:
        title: original query title.
        slug: slug that produced the hit (useful for
            debugging which variant matched).
        metascore: critic score (0-100) or ``None``.
        user_score: user score (0.0-10.0) or ``None``.
        description: sanitized description.
        url: canonical Metacritic page URL.
    """

    title: str
    slug: str
    metascore: int | None
    user_score: float | None
    description: str
    url: str

    def to_dict(self) -> dict:
        """Serialise to a JSON-friendly dict.

        Returns:
            Six-key dict.
        """
        return {
            "title": self.title,
            "slug": self.slug,
            "metascore": self.metascore,
            "user_score": self.user_score,
            "description": self.description,
            "url": self.url,
        }


async def fetch_score(title: str, config: ConfigManager | None = None) -> MetacriticScore | None:
    """Try every slug candidate and return the first successful score.

    Generates candidates via ``_slug_candidates`` then
    iterates until one returns a parseable response.
    No early termination on non-200 — the composer API
    returns 404 on slug miss, which is the expected
    "try next" signal.

    Args:
        title: human-readable title.
        config: optional ``ConfigManager`` for URL +
            timeout override.

    Returns:
        ``MetacriticScore`` on first hit, ``None`` if
        no candidate matched.
    """
    composer_url = get_cfg(
        config,
        "metadata.metacritic.composer_url",
        "https://backend.metacritic.com/composer/metacritic/pages/games/{slug}/web",
    )
    timeout = get_cfg(
        config,
        "metadata.metacritic.fetch_timeout_seconds",
        15,
    )
    candidates = _slug_candidates(title)
    logger.debug("[metacritic] %d slug candidates for %r", len(candidates), title)
    for slug in candidates:
        data = await _fetch_composer(slug, composer_url, timeout)
        if data is None:
            continue
        score = _parse_composer_response(title, slug, data)
        if score is not None:
            return score
    return None


def _cfg(config: ConfigManager | None, key: str, default: Any) -> Any:
    """Thin wrapper around ``get_cfg`` — local convention.

    Args:
        config: optional ``ConfigManager``.
        key: dotted config key.
        default: fallback.

    Returns:
        Config value or default.
    """
    return get_cfg(config, key, default)


def _slug_candidates(title: str) -> list[str]:
    """Enumerate the slug candidates derived from ``title``.

    Walks the four transforms (raw, clean, suffix-
    stripped, numeral variants) into a set (dedup),
    then slugifies each. Empty / whitespace-only
    variants are filtered out.

    Order matters: the more aggressive transforms
    (suffix-stripped, numeral-swapped) come last, so
    if multiple variants match Metacritic, the most
    literal one wins.

    Args:
        title: original title.

    Returns:
        Ordered list of slug strings.
    """
    variants = {title}
    cleaned = clean_title(title)
    variants.add(cleaned)
    variants.add(strip_suffixes(cleaned))
    for v in list(variants):
        for alt in get_numeral_variants(v):
            variants.add(alt)
    return [slugify_game_name(v) for v in variants if v.strip()]


async def _fetch_composer(slug: str, url_template: str, timeout: int) -> dict | None:
    """GET the composer API for ``slug`` and return the parsed JSON.

    Returns ``None`` on any failure (non-200, network
    error, JSON decode error). Failures log at DEBUG
    only — slug misses are expected during the
    candidate walk.

    Args:
        slug: candidate slug.
        url_template: URL template with ``{slug}``
            placeholder.
        timeout: HTTP timeout in seconds.

    Returns:
        Parsed JSON dict, or ``None``.
    """
    import aiohttp

    url = url_template.format(slug=slug)
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(url, timeout=timeout) as resp,
        ):
            if resp.status != 200:
                return None
            return cast("dict[Any, Any] | None", await resp.json())
    except Exception as e:
        logger.debug(
            "[metacritic] fetch(%s) failed: %s",
            slug,
            e,
        )
        return None


def _parse_composer_response(title: str, slug: str, data: dict) -> MetacriticScore | None:
    """Extract the score fields from a composer-API response.

    The composer response shape:

    * ``data.components`` — list of components;
    * find the one with ``type="gameInfo"``;
    * its ``data.item`` carries
      ``criticScoreSummary.score``,
      ``userScoreSummary.score``, and ``description``.

    Defensive ``except (AttributeError, TypeError,
    KeyError)`` covers all the missing/typed-wrong
    failure modes from external JSON. Failures log at
    DEBUG.

    Args:
        title: original query (for the score object).
        slug: the slug that hit (for the score object).
        data: parsed composer response.

    Returns:
        ``MetacriticScore`` on success, ``None`` on
        unparseable response.
    """
    try:
        components = data.get("components", [])
        game_info = next(
            (c for c in components if c.get("type") == "gameInfo"),
            None,
        )
        if not game_info:
            return None
        payload = game_info.get("data", {}).get("item", {})
        if not payload:
            return None
        return MetacriticScore(
            title=title,
            slug=slug,
            metascore=payload.get(
                "criticScoreSummary",
                {},
            ).get("score"),
            user_score=payload.get(
                "userScoreSummary",
                {},
            ).get("score"),
            description=sanitize_description(
                payload.get("description", ""),
            ),
            url=f"https://www.metacritic.com/game/{slug}/",
        )
    except (AttributeError, TypeError, KeyError) as e:
        logger.debug("[metacritic] parse error: %s", e)
        return None
