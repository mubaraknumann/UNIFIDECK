"""LunaCatalog: catalog models, DOM-scrape source, and sync reconciliation.

This package replaces the former single ``luna_catalog.py`` module (it outgrew
the 550-LOC volumetry cap). It is split by responsibility:

* :mod:`.models` — the frozen data models and the :class:`CatalogSource`
  strategy boundary (task 8.2).
* :mod:`.parsing` — the pure DOM-scrape parsing + :class:`LunaCatalogScrapeError`
  (task 8.3/8.4).
* :mod:`.scrape_source` — :class:`DomScrapeCatalogSource`, the impure CDP
  driver (Strategy B).
* :mod:`.sync` — :class:`LunaCatalog` + the sync-status/result types, the
  catalog writer and library-cache reconciliation (task 8.5).

The public surface is re-exported here, so the historical import path
(``from unifideck.stores.luna.luna_catalog import <name>`` and
``import unifideck.stores.luna.luna_catalog as catalog_mod``) keeps working
unchanged. Note ``DomScrapeCatalogSource`` is intentionally the only concrete
``*CatalogSource`` exported — there is no ``ApiCatalogSource`` until a Luna
endpoint is confirmed (design Q4).
"""
from __future__ import annotations

from unifideck.stores.luna.luna_catalog.models import (
    DEFAULT_CATALOG_STRATEGY,
    LUNA_STORE_TAG,
    CatalogSource,
    CatalogSourceKind,
    CatalogStrategy,
    EntitlementSet,
    LunaTitle,
)
from unifideck.stores.luna.luna_catalog.parsing import (
    PRIME_INCLUDED_MARKER,
    LunaCatalogScrapeError,
    compose_detail_url,
    extract_cards_from_evaluate,
    parse_library_cards,
)
from unifideck.stores.luna.luna_catalog.scrape_source import (
    DomScrapeCatalogSource,
)
from unifideck.stores.luna.luna_catalog.sync import (
    CatalogSyncResult,
    CatalogSyncStatus,
    LunaCatalog,
)

__all__ = [
    "DEFAULT_CATALOG_STRATEGY",
    "LUNA_STORE_TAG",
    "PRIME_INCLUDED_MARKER",
    "CatalogSource",
    "CatalogSourceKind",
    "CatalogStrategy",
    "CatalogSyncResult",
    "CatalogSyncStatus",
    "DomScrapeCatalogSource",
    "EntitlementSet",
    "LunaCatalog",
    "LunaCatalogScrapeError",
    "LunaTitle",
    "compose_detail_url",
    "extract_cards_from_evaluate",
    "parse_library_cards",
]
