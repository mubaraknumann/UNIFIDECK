"""UnifiDB lookup — bucketed metadata fetch + fuzzy title matching.

OP-20a | py_modules/unifideck/metadata/unifidb.py

UnifiDB is the project's own crowd-sourced metadata
database hosted on GitHub
(``mubaraknumann/unifiDB``) and served via jsDelivr CDN.
Games are bucketed by their first two letters
(``aa.json``, ``ab.json``, …) so each fetch downloads
only ~1% of the corpus.

Lookup pipeline:

1. Normalise the title + pick the bucket
   (``get_first_char_for_bucket``);
2. Fetch the bucket from the CDN (``_fetch_bucket``);
3. Try exact match on ``external_ids.<store>`` first
   (authoritative);
4. Fall back to fuzzy title matching with a configurable
   score threshold (``MATCH_THRESHOLD``).

The scoring algorithm combines containment +
Jaccard token similarity — empirical choices that work
well on the typical store-vs-canonical title noise
(trademark symbols, "Edition" suffixes, "the" prefix,
etc.).
"""

from __future__ import annotations

import logging
import re
import string
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..utils.config_helpers import get_cfg

if TYPE_CHECKING:
    from ..config import ConfigManager

logger = logging.getLogger(__name__)

UNIFIDB_CDN_BASE = "https://cdn.jsdelivr.net/gh/mubaraknumann/unifiDB@main"
MATCH_THRESHOLD = 0.65


def normalize_title_for_matching(title: str) -> str:
    """Strip noise (case, trademarks, punctuation, whitespace) for matching.

    Three-step strip:

    1. Lower-case (case-insensitive matches);
    2. Drop ™ and ® unicode characters;
    3. Strip ASCII punctuation;
    4. Collapse internal whitespace to single space.

    Used by ``score_title_match`` + ``cross_store_dedup``
    so different stores' rendering of the same game
    (``"Halo Infinite™"`` vs ``"halo: infinite"``) match.

    Args:
        title: raw title string.

    Returns:
        Normalised lowercase string, no punctuation, no
        trademarks.
    """
    title = title.lower()
    title = re.sub(r"[\u2122\u00AE]", "", title)
    title = title.translate(
        str.maketrans("", "", string.punctuation),
    )
    title = re.sub(r"\s+", " ", title)
    return title.strip()


def get_first_char_for_bucket(title: str) -> str:
    """Return the two-letter bucket name for ``title``.

    Three-arm bucketing:

    * Strip leading articles (``"the"``, ``"a"``,
      ``"an"``) so ``"The Witcher"`` buckets like
      ``"Witcher"``;
    * Empty / non-alpha first char → bucket ``"0_9"``;
    * Otherwise: first letter + second letter (or
      first letter doubled when title is single-char).

    Examples:

    * ``"The Witcher"`` → ``"wi"``
    * ``"Halo"``        → ``"ha"``
    * ``"3D Pinball"``  → ``"0_9"``

    Args:
        title: raw title.

    Returns:
        Two-character bucket key (lowercase), or
        ``"0_9"`` for digits / non-alpha starts.
    """
    normalized = normalize_title_for_matching(title)
    for article in ("the ", "a ", "an "):
        if normalized.startswith(article):
            normalized = normalized[len(article) :]
            break
    if not normalized:
        return "0_9"
    first = normalized[0]
    if not first.isalpha():
        return "0_9"
    second = normalized[1] if len(normalized) > 1 and normalized[1].isalnum() else first
    return f"{first}{second}"


def score_title_match(search: str, candidate: str) -> float:
    """Return a 0.0..1.0 match score between two normalised titles.

    Three-arm scoring (first matching arm wins):

    * **Exact match** after normalisation → 1.0.
    * **Substring containment** (one is prefix/substring
      of the other) → 0.85, but only when the longer
      title isn't more than 2× the shorter (avoids
      false positives like
      ``"halo"`` ⊂ ``"halo: master chief collection bundle"``).
    * **Jaccard token similarity** scaled by 0.8 — the
      common fallback. Captures shared keywords with
      different ordering.

    Args:
        search: query title.
        candidate: candidate title from the DB.

    Returns:
        Score in [0.0, 1.0]; higher = better match.
    """
    a = normalize_title_for_matching(search)
    b = normalize_title_for_matching(candidate)
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    if a in b or b in a:
        shorter = min(len(a), len(b))
        longer = max(len(a), len(b))
        if longer <= 2 * shorter:
            return 0.85
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersect = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return 0.8 * (len(intersect) / len(union))


def extract_store_id(game: dict[str, Any], store: str) -> str | None:
    """Return the store-specific external id from a UnifiDB game record.

    UnifiDB games carry ``external_ids`` — a dict
    keyed by store name with the game's id on each
    store. Defensive: returns ``None`` if the field is
    missing, isn't a dict, or doesn't contain the
    requested store.

    Args:
        game: a UnifiDB game record.
        store: store identifier.

    Returns:
        Stringified store id, or ``None``.
    """
    external = game.get("external_ids") or {}
    if not isinstance(external, dict):
        return None
    val = external.get(store)
    return str(val) if val is not None else None


def get_best_match(search_title: str, candidates: list[dict[str, Any]], threshold: float = MATCH_THRESHOLD) -> dict[str, Any] | None:
    """Pick the highest-scoring candidate above the threshold.

    Scores every candidate via ``score_title_match``,
    filters by threshold, sorts descending, returns
    the top one. No score → ``None``. Stable for ties
    (Python's ``sort`` is stable so the first
    high-scoring candidate wins).

    Args:
        search_title: query.
        candidates: list of UnifiDB game records.
        threshold: minimum acceptable score.

    Returns:
        Best matching game record, or ``None`` if
        nothing met the threshold.
    """
    if not candidates:
        return None
    scored: list[tuple[float, dict[str, Any]]] = []
    for c in candidates:
        name = c.get("title") or c.get("name") or ""
        score = score_title_match(search_title, name)
        if score >= threshold:
            scored.append((score, c))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def game_to_cache_format(game: dict[str, Any]) -> dict[str, Any]:
    """Slim down a raw UnifiDB record to just the cached fields.

    UnifiDB records have many fields; the cache only
    keeps the seven used by the metadata service.
    Centralising the field list here means cache
    upgrades happen in one place.

    ``title`` falls back through ``name`` so older
    UnifiDB records (which used ``name``) still match.

    Args:
        game: raw UnifiDB record.

    Returns:
        Cache-format dict.
    """
    return {
        "title": game.get("title") or game.get("name") or "",
        "description": game.get("description", ""),
        "release_date": game.get("release_date", ""),
        "publisher": game.get("publisher", ""),
        "developers": game.get("developers", []),
        "genres": game.get("genres", []),
        "platforms": game.get("platforms", []),
        "external_ids": game.get("external_ids", {}),
    }


@dataclass
class UnifiDBResult:
    """Typed shape for a successful UnifiDB lookup.

    Attributes:
        title: canonical title.
        description: prose description.
        release_date: ISO-8601 date string.
        publisher: publishing entity.
        developers: list of developer studios.
        genres: list of genre tags.
    """

    title: str
    description: str
    release_date: str
    publisher: str
    developers: list[str]
    genres: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict.

        Field-by-field copy (rather than
        ``dataclasses.asdict``) for consistency with
        other typed records — explicit field control on
        the wire format.

        Returns:
            Six-key dict.
        """
        return {
            "title": self.title,
            "description": self.description,
            "release_date": self.release_date,
            "publisher": self.publisher,
            "developers": self.developers,
            "genres": self.genres,
        }


async def lookup(store: str, game_id: str, title: str, config: ConfigManager | None = None) -> dict[str, Any] | None:
    """Look up a game in UnifiDB by id first, then by fuzzy title.

    Two-stage match:

    1. **ID match** — if the game's bucket contains an
       entry with ``external_ids.<store>`` matching
       ``game_id``, return it. Authoritative.
    2. **Title match** — fall back to fuzzy title via
       ``get_best_match`` with the configured
       threshold.

    Config keys (all optional with documented defaults):

    * ``metadata.unifidb.cdn_base`` — CDN base URL;
    * ``metadata.unifidb.match_threshold`` — fuzzy
      threshold;
    * ``metadata.unifidb.fetch_timeout_seconds`` —
      HTTP timeout.

    Args:
        store: store identifier (for id match).
        game_id: store-native game id.
        title: title for bucket selection + fuzzy
            match.
        config: optional ``ConfigManager``.

    Returns:
        Cache-format dict on hit, ``None`` if no match
        in the bucket.
    """
    cdn_base = get_cfg(
        config,
        "metadata.unifidb.cdn_base",
        UNIFIDB_CDN_BASE,
    )
    threshold = get_cfg(
        config,
        "metadata.unifidb.match_threshold",
        MATCH_THRESHOLD,
    )
    timeout = get_cfg(
        config,
        "metadata.unifidb.fetch_timeout_seconds",
        15,
    )
    bucket = get_first_char_for_bucket(title)
    games = await _fetch_bucket(bucket, cdn_base, timeout)
    if not games:
        return None
    for game in games:
        if extract_store_id(game, store) == game_id:
            logger.debug(
                "[unifidb] id match: %s:%s",
                store,
                game_id,
            )
            return game_to_cache_format(game)
    best = get_best_match(title, games, threshold)
    if best:
        logger.debug("[unifidb] title match: %r", title)
        return game_to_cache_format(best)
    return None


async def _fetch_bucket(bucket: str, cdn_base: str, timeout: int) -> list[dict[str, Any]]:
    """Fetch one bucket's JSON from the CDN, returning ``[]`` on failure.

    URL layout: ``<base>/games/<first-letter>/<bucket>.json``
    — sub-pathed by first letter to keep each directory
    listing small.

    Failure tolerance is broad: any exception (network,
    JSON decode, non-200 status) returns ``[]`` with a
    DEBUG log. Callers treat empty bucket and fetch
    failure identically (no match).

    Args:
        bucket: two-letter bucket id.
        cdn_base: CDN URL prefix.
        timeout: HTTP timeout in seconds.

    Returns:
        List of game records, or ``[]`` on any failure.
    """
    import aiohttp

    first_char = bucket[0] if bucket else "0_9"
    url = f"{cdn_base}/games/{first_char}/{bucket}.json"
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(url, timeout=timeout) as resp,
        ):
            if resp.status != 200:
                return []
            data = await resp.json()
            if isinstance(data, list):
                return data
            return []
    except Exception as e:
        logger.debug(
            "[unifidb] fetch(%s) failed: %s",
            url,
            e,
        )
        return []
