"""ProtonDB + Deck Verified rating fetcher with on-disk cache.

OP-12a | py_modules/unifideck/compatibility/library.py

Aggregates two independent compatibility signals for a
Steam AppID:

* **ProtonDB tier** — community-reported Proton
  compatibility (``platinum`` / ``gold`` / ``silver`` /
  ``bronze`` / ``borked``);
* **Steam Deck Verified status** — official Valve
  category (``verified`` / ``playable`` / ``unsupported``
  / ``unknown``).

Combined into a single ``CompatRating`` record that
captures both + a ``sources`` list documenting which
fetches succeeded.

Results are cached in a dedicated ``compat`` cache
namespace with a 7-day default TTL — compatibility data
changes slowly, no need to hit external APIs on every
sync.

The module also re-exports a handful of legacy
free-function aliases (``load_compat_cache``,
``save_compat_cache``, ``fetch_protondb_rating``, etc.)
for backward compatibility with code that pre-dated the
class-based API.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config import ConfigManager
    from ..core.cache_manager import CacheManager

from ..utils.config_helpers import get_cfg

logger = logging.getLogger(__name__)

PROTONDB_TIERS = ("platinum", "gold", "silver", "bronze", "borked")
DECK_CATEGORIES = {
    0: "unknown",
    1: "unsupported",
    2: "playable",
    3: "verified",
}
PROTONDB_URL = "https://www.protondb.com/api/v1/reports/summaries/{appid}.json"
DECK_VERIFIED_URL = (
    "https://store.steampowered.com/saleaction/"
    "ajaxgetdeckappcompatibilityreport?nAppID={appid}"
)
DEFAULT_USER_AGENT = "Unifideck/1.0 (compat-library)"
CACHE_NAMESPACE = "compat"


@dataclass
class CompatRating:
    """Aggregated compatibility rating for one game.

    Attributes:
        appid: Steam AppID (None for title-only lookups
            that failed to resolve to a Steam app).
        title: original query title.
        protondb_tier: one of ``PROTONDB_TIERS`` or
            ``None`` if not on ProtonDB.
        deck_status: one of ``DECK_CATEGORIES`` values;
            ``"unknown"`` when the fetch failed.
        sources: list of source IDs that contributed
            data; lets the UI display which signals
            are available.
        error: free-form error string on lookup
            failure (e.g. ``"not_found_on_steam_store"``).
    """

    appid: int | None = None
    title: str = ""
    protondb_tier: str | None = None
    deck_status: str = "unknown"
    sources: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict.

        Returns:
            Six-key dict.
        """
        return {
            "appid": self.appid,
            "title": self.title,
            "protondb_tier": self.protondb_tier,
            "deck_status": self.deck_status,
            "sources": list(self.sources),
            "error": self.error,
        }


def parse_protondb_response(payload: dict[str, Any]) -> str | None:
    """Extract the tier string from a ProtonDB API payload.

    Returns ``None`` if the payload isn't a dict, the
    ``tier`` field is missing, or the value isn't in
    the known tier set — defensive against API drift.

    Args:
        payload: parsed JSON response.

    Returns:
        Tier string from ``PROTONDB_TIERS``, or
        ``None``.
    """
    if not isinstance(payload, dict):
        return None
    tier = payload.get("tier")
    if isinstance(tier, str) and tier in PROTONDB_TIERS:
        return tier
    return None


def parse_deck_verified_response(payload: dict[str, Any]) -> str:
    """Extract the deck-status string from a Steam Deck verified payload.

    The Steam response wraps the category int inside
    ``results.resolved_category`` — walked defensively
    so any malformed response cleanly degrades to
    ``"unknown"`` rather than raising.

    Args:
        payload: parsed JSON response.

    Returns:
        Status string from ``DECK_CATEGORIES`` values.
    """
    if not isinstance(payload, dict):
        return "unknown"
    results = payload.get("results")
    if not isinstance(results, dict):
        return "unknown"
    cat = results.get("resolved_category", 0)
    try:
        return DECK_CATEGORIES.get(int(cat), "unknown")
    except (TypeError, ValueError):
        return "unknown"


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


class CompatLibrary:
    """Cached compatibility-rating fetcher.

    Wraps the two external APIs (ProtonDB + Steam
    Deck Verified) with a per-AppID cache and convenient
    title-based + bulk lookup helpers.
    """

    def __init__(self, cache: CacheManager | None = None, config: ConfigManager | None = None) -> None:
        """Bind the cache + config and register the compat cache namespace.

        Cache registration is best-effort: if the cache
        manager is missing or registration throws (e.g.
        already registered), the library still works
        but without persistence.

        TTL defaults to 7 days (604800 s) — compatibility
        ratings change slowly.

        Args:
            cache: optional ``CacheManager``.
            config: optional ``ConfigManager``.
        """
        self._cache = cache
        self._config = config
        if cache is not None:
            ttl = int(get_cfg(config, "cache_ttl.compat", 604800))
            try:
                cache.register(CACHE_NAMESPACE, ttl_seconds=ttl)
            except Exception:
                pass

    async def get_for_appid(self, appid: int) -> CompatRating:
        """Return cached or freshly-fetched rating for a Steam AppID.

        Cache-first: a hit returns immediately as a
        rehydrated ``CompatRating``. Otherwise fetches
        both ProtonDB and Deck Verified in sequence
        (not parallel — keeps source ordering deterministic
        and avoids hammering both APIs simultaneously on
        bulk fetches).

        The ``sources`` list tracks which fetches
        contributed real data (vs default).

        Args:
            appid: Steam AppID.

        Returns:
            ``CompatRating`` (possibly with empty
            tier/status if both fetches failed).
        """
        cached = self._cache_get(str(appid))
        if cached is not None:
            return CompatRating(**cached)
        result = CompatRating(appid=appid)
        result.protondb_tier = await self._fetch_protondb(appid)
        if result.protondb_tier is not None:
            result.sources.append("protondb")
        result.deck_status = await self._fetch_deck_verified(appid)
        if result.deck_status != "unknown":
            result.sources.append("deck_verified")
        self._cache_set(str(appid), result.to_dict())
        return result

    async def get_for_title(self, title: str) -> CompatRating:
        """Resolve ``title`` to an AppID via Steam store search, then fetch.

        Two-step:

        1. ``search_store`` from the steam package to
           find the AppID;
        2. delegate to ``get_for_appid``.

        If the Steam search fails, returns a
        ``CompatRating`` carrying the title + an
        ``error`` field — keeps the public API
        consistent (always returns a record, never
        ``None``).

        Args:
            title: human-readable title.

        Returns:
            ``CompatRating``.
        """
        from ..steam.library import search_store

        steam = await search_store(title, config=self._config)
        if steam is None or "app_id" not in steam:
            return CompatRating(
                title=title,
                error="not_found_on_steam_store",
            )
        result = await self.get_for_appid(int(steam["app_id"]))
        result.title = title
        return result

    async def bulk_fetch(self, titles: list[str], delay_ms: int = 50) -> dict[str, CompatRating]:
        """Fetch ratings for many titles with a courtesy delay between calls.

        Sequential (not parallel) so the external APIs
        aren't flooded — ProtonDB and Steam both rate-limit
        aggressively. The 50 ms default delay translates
        to ~20 lookups per second, well below typical
        thresholds.

        Args:
            titles: list of titles.
            delay_ms: per-title delay in milliseconds
                (set to 0 to disable).

        Returns:
            ``{title: CompatRating}`` dict (preserves
            input order on CPython 3.7+).
        """
        out: dict[str, CompatRating] = {}
        for title in titles:
            out[title] = await self.get_for_title(title)
            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000)
        return out

    async def _fetch_protondb(self, appid: int) -> str | None:
        """GET ProtonDB summary for one AppID, parsing to a tier string.

        Returns ``None`` on 404 (game not on ProtonDB —
        expected for niche titles) or any other failure.
        Failure logs at DEBUG only — bulk fetches see
        lots of 404s and they're not actionable.

        Args:
            appid: Steam AppID.

        Returns:
            Tier string or ``None``.
        """
        import aiohttp

        url = PROTONDB_URL.format(appid=appid)
        timeout = int(
            _cfg(
                self._config,
                "compat.protondb_timeout_seconds",
                30,
            )
        )
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    headers={"User-Agent": DEFAULT_USER_AGENT},
                    timeout=timeout,
                ) as resp,
            ):
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    return None
                return parse_protondb_response(
                    await resp.json(),
                )
        except Exception as e:
            logger.debug(
                "[compat] protondb(%d) failed: %s",
                appid,
                e,
            )
            return None

    async def _fetch_deck_verified(self, appid: int) -> str:
        """GET Steam's Deck Verified report for one AppID.

        Returns ``"unknown"`` on any failure — keeps
        callers from having to distinguish "not on
        Steam" from "API failure".

        Shorter default timeout (10 s vs 30 s for
        ProtonDB) because Steam's API is usually fast.

        Args:
            appid: Steam AppID.

        Returns:
            Status string from ``DECK_CATEGORIES``
            values.
        """
        import aiohttp

        url = DECK_VERIFIED_URL.format(appid=appid)
        timeout = int(
            _cfg(
                self._config,
                "compat.deck_verified_timeout_seconds",
                10,
            )
        )
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    headers={"User-Agent": DEFAULT_USER_AGENT},
                    timeout=timeout,
                ) as resp,
            ):
                if resp.status != 200:
                    return "unknown"
                return parse_deck_verified_response(
                    await resp.json(),
                )
        except Exception as e:
            logger.debug(
                "[compat] deck(%d) failed: %s",
                appid,
                e,
            )
            return "unknown"

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        """Cache wrapper — safe read, returns ``None`` on missing cache or error.

        Args:
            key: cache key (typically stringified AppID).

        Returns:
            Cached dict or ``None``.
        """
        if self._cache is None:
            return None
        try:
            return self._cache.get(CACHE_NAMESPACE, key)
        except Exception:
            return None

    def _cache_set(
        self,
        key: str,
        value: dict[str, Any],
    ) -> None:
        """Cache wrapper — safe write, silently ignores errors.

        Args:
            key: cache key.
            value: value to store.
        """
        if self._cache is None:
            return
        try:
            self._cache.set(CACHE_NAMESPACE, key, value)
        except Exception:
            pass


def load_compat_cache():
    """Legacy stub — pre-class-API callers expected an in-memory dict.

    Returns an empty dict for callers that haven't
    migrated to ``CompatLibrary``. Logs at DEBUG once
    per call site so the migration progress is visible.

    Returns:
        Empty dict.
    """
    logger.debug("[compat] load_compat_cache called via legacy path")
    return {}


def save_compat_cache(cache):
    """Legacy stub — no-op since persistence is handled by ``CacheManager``.

    Args:
        cache: ignored (legacy in-memory dict).

    Returns:
        ``True`` (legacy success contract).
    """
    logger.debug("[compat] save_compat_cache called via legacy path")
    return True


async def search_steam_store(session=None, title="", **kwargs):
    """Legacy alias for ``steam.library.search_store``.

    The ``session`` arg is accepted for API
    compatibility but unused — ``search_store`` builds
    its own client.

    Args:
        session: ignored (legacy aiohttp session).
        title: query title.
        **kwargs: ignored.

    Returns:
        Whatever ``search_store`` returns.
    """
    from ..steam.library import search_store

    return await search_store(title)


async def fetch_protondb_rating(session=None, appid=0, **kwargs):
    """Legacy free-function — builds a one-shot ``CompatLibrary`` to do the fetch.

    Functional but suboptimal: defeats the cache (each
    call constructs a fresh library). New code should
    use ``CompatLibrary`` directly.

    Args:
        session: ignored.
        appid: Steam AppID.
        **kwargs: ignored.

    Returns:
        Tier string or ``None``.
    """
    lib = CompatLibrary()
    return await lib._fetch_protondb(int(appid))


async def fetch_deck_verified(session=None, appid=0, **kwargs):
    """Legacy free-function for Deck Verified lookup.

    Same caveat as ``fetch_protondb_rating``: no caching.

    Args:
        session: ignored.
        appid: Steam AppID.
        **kwargs: ignored.

    Returns:
        Status string.
    """
    lib = CompatLibrary()
    return await lib._fetch_deck_verified(int(appid))


async def get_compat_for_title(session=None, title="", **kwargs):
    """Legacy free-function returning ``(status, dict)`` rather than a dataclass.

    Wraps ``CompatLibrary.get_for_title``; converts the
    error field into a status string (``"ok"`` on
    success, the error code otherwise) and returns a
    tuple.

    Args:
        session: ignored.
        title: query.
        **kwargs: ignored.

    Returns:
        Tuple ``(status_string, rating_dict)``.
    """
    lib = CompatLibrary()
    rating = await lib.get_for_title(title)
    status = "ok" if rating.error is None else rating.error
    return (status, rating.to_dict())


async def prefetch_compat(titles, _batch_size=10, delay_ms=50):
    """Legacy bulk prefetch — builds a one-shot library and runs ``bulk_fetch``.

    The ``_batch_size`` arg is accepted for API
    compatibility but unused — the new bulk fetcher
    doesn't batch internally.

    Args:
        titles: iterable of titles.
        _batch_size: ignored.
        delay_ms: per-title delay forwarded to
            ``bulk_fetch``.

    Returns:
        ``{title: CompatRating}``.
    """
    lib = CompatLibrary()
    return await lib.bulk_fetch(list(titles), delay_ms=delay_ms)


class BackgroundCompatFetcher:
    """Legacy background-fetch shim — kept for API stability.

    The original implementation owned its own task
    loop; the new design pushes that responsibility
    onto the caller's service. The class is kept as a
    pass-through so external code doesn't break.
    """

    def __init__(self, *args, **kwargs):
        """Build the inner library; ignore all other args.

        Args:
            *args / **kwargs: ignored (legacy
                signature preserved).
        """
        self._lib = CompatLibrary()

    def start(self):
        """No-op (legacy lifecycle API).

        Kept on the surface so callers don't break.
        Actual fetching now happens on demand via
        ``fetch``.
        """
        pass

    def stop(self):
        """No-op (legacy lifecycle API).

        Kept for back-compat with callers
        from older versions that expect a
        symmetric ``start/stop`` pair on
        adapter objects. The on-demand
        fetch model means there's no
        background task to tear down.
        """
        pass

    async def fetch(self, title):
        """Forward to the inner ``CompatLibrary.get_for_title``.

        Args:
            title: query.

        Returns:
            ``CompatRating``.
        """
        return await self._lib.get_for_title(title)
