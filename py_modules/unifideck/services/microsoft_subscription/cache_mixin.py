"""Cache mixin for the subscription service — read/write/invalidate operations against the CacheManager-backed store."""

from __future__ import annotations
import logging
import time
from typing import TYPE_CHECKING
from .cache import _CachedEntry
from .constants import _CACHE_KEY_PREFIX, _CACHE_STORE_NAME
from .time_utils import _end_of_month_utc
if TYPE_CHECKING:
    from ...core.cache_manager import CacheManager
    from ...core.types import SubscriptionTier
    from ...stores.microsoft.tokens import (
        MicrosoftTokenManager,
        XBLTokenChain,
    )
logger = logging.getLogger(__name__)
class _CacheMixin:
    """Cache mixin."""
    _cache: CacheManager
    _last_standard_chain: XBLTokenChain | None
    async def _resolve_cache_key(
        self,
        token_manager: MicrosoftTokenManager,
    ) -> str:
        """Build a cache key for the current Microsoft account.

        Builds the XBL token chain to extract the xuid and uses it
        as the per-account cache namespace. Falls back to
        ``default`` if the chain can't be built.

        Args:
            token_manager: Microsoft token manager.

        Returns:
            Cache key string ``<prefix><xuid|"default">``.
        """
        xuid: str | None = None
        try:
            chain = await token_manager.build_chain()
            if chain is not None:
                xuid = chain.xuid
                self._last_standard_chain = chain
        except Exception:
            logger.debug(
                "[MSSubSvc] could not build chain for key resolution",
                exc_info=True,
            )
        return f"{_CACHE_KEY_PREFIX}{xuid or 'default'}"
    def _read_cache(self, key: str) -> _CachedEntry | None:
        """Read the cached subscription entry for one key.

        Args:
            key: Cache key from ``_resolve_cache_key``.

        Returns:
            The cached ``_CachedEntry``, or ``None`` if absent,
            unreadable, or malformed.
        """
        try:
            raw = self._cache.get(_CACHE_STORE_NAME, key)
        except Exception:
            logger.exception("[MSSubSvc] cache read failed")
            return None
        if raw is None:
            return None
        if isinstance(raw, dict):
            return _CachedEntry.from_dict(raw)
        return None
    def _write_cache(self, key: str, entry: _CachedEntry) -> None:
        """Persist one subscription entry under ``key`` in the cache.

        Failures are logged but not raised — the cache is
        best-effort.

        Args:
            key: Cache key.
            entry: Entry to store.
        """
        try:
            self._cache.set(_CACHE_STORE_NAME, key, entry.to_dict())
        except Exception:
            logger.exception("[MSSubSvc] cache write failed")
    async def _store_tier_result(
        self, cache_key: str, tier: SubscriptionTier,
    ) -> None:
        """Persist a freshly-probed subscription tier and notify subscribers.

        Builds a ``_CachedEntry`` with the current detection time
        and end-of-month UTC expiry, writes it to the cache, and
        emits the state-change event for the new tier.

        Args:
            cache_key: Per-account cache key.
            tier: Newly-detected subscription tier.
        """
        entry = _CachedEntry(
            tier=tier,
            expires_at=_end_of_month_utc(),
            detected_at=time.time(),
        )
        self._write_cache(cache_key, entry)
        await self._emit_state_change(cache_key, tier)