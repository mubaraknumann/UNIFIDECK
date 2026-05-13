"""Default cache registration at plugin boot.

OP-23a | py_modules/unifideck/bootstrap/cache_registry.py

Declares the full set of named caches the plugin needs and
registers each one with the ``CacheManager`` during boot.
Centralising the list here means caches are documented in
one place + TTL changes are reviewed in a single PR.

Two tables:

* ``_NAMED_CACHES``  — per-feature caches with their TTLs
  (in seconds; 0 = never expire). Metadata caches use
  one-day or one-week TTLs; lookup caches (Steam AppID
  resolution) are TTL=0 because they're authoritative.
* ``_STORE_CACHES``  — one cache per supported store, all
  TTL=0 (the store-side code handles freshness via its
  own sync mechanism).
"""

from __future__ import annotations

from typing import Any

_NAMED_CACHES: tuple[tuple[str, int], ...] = (
    ("steam_appid", 0),
    ("steam_real_appid", 0),
    ("steam_metadata", 86400),
    ("rawg_metadata", 86400),
    ("unifidb_metadata", 86400),
    ("metacritic", 604800),
    ("artwork_attempts", 0),
    ("game_sizes", 3600),
    ("compat", 0),
)

_STORE_CACHES: tuple[str, ...] = (
    "epic",
    "gog",
    "amazon",
    "microsoft",
    "ubisoft",
)


def register_default_caches(cache: Any) -> None:
    """Call ``cache.register`` for every cache the plugin needs.

    Idempotent on the cache manager's side
    (``CacheManager.register`` no-ops on duplicate
    names), so safe to call multiple times during dev
    workflows.

    Order isn't significant for correctness but matches
    the declaration order in the tables for readable
    plugin logs.

    Args:
        cache: live ``CacheManager`` instance.
    """
    for name, ttl in _NAMED_CACHES:
        cache.register(name, ttl_seconds=ttl)
    for store_name in _STORE_CACHES:
        cache.register(store_name, ttl_seconds=0)
