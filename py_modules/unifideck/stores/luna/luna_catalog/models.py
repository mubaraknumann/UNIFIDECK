"""Luna catalog data models and the source-strategy boundary (task 8.2).

Defines the frozen data models (:class:`LunaTitle`, :class:`EntitlementSet`)
and the pluggable :class:`CatalogSource` Protocol for Luna catalog/entitlement
acquisition (Req 3.1, 4.1). It deliberately stops at the interface: the
Strategy B DOM scrape and the ``LunaSubscription``/reconciliation glue live in
sibling modules of this package.

Catalog/entitlement acquisition is the lowest-confidence, highest-risk part of
the Luna feature (design Open Question Q4): the feasibility test exercised
**no** Luna catalog or entitlement API, so the data source is a pluggable
strategy with two intended implementations — Strategy A (authenticated API,
unverified) and Strategy B (CDP-driven DOM scrape, the default). Every result
records *which* path produced it via :attr:`EntitlementSet.source`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from unifideck.auth.edge_browser.cdp_client import EdgeCDPClient
    from unifideck.stores.luna.luna_config import LunaConfig

#: The store tag every Luna library entry additionally carries (Req 4.5),
#: alongside ``GameTag.BROWSER``. Drives the frontend's Luna Play control and
#: the per-store reconcile.
LUNA_STORE_TAG = "luna"

#: Which acquisition path produced a set of catalog/entitlement data. Also
#: the ``EntitlementSet.source`` value space. ``"none"`` marks a result
#: that no path could produce (e.g. the entitlement source was
#: unreachable), kept distinct from an *empty* API/scrape answer so the
#: store can map it to ``entitlement_source_unavailable`` rather than
#: ``subscription_required`` (design "Selection and risk handling",
#: Req 3.6).
CatalogSourceKind = Literal["api", "dom-scrape", "none"]


class CatalogStrategy(Enum):
    """The selectable catalog-acquisition strategies (design Q4).

    ``STRATEGY_B`` (DOM scrape) is the default until a Luna catalog/
    entitlement API is verified; ``STRATEGY_A`` (authenticated API) can be
    selected once a real endpoint is confirmed. The enum value is the
    :data:`CatalogSourceKind` a strategy stamps onto its results.
    """

    STRATEGY_A = "api"
    STRATEGY_B = "dom-scrape"


#: The strategy selected until a Luna catalog/entitlement API is verified
#: (design "Selection and risk handling"): DOM scrape, because it uses only
#: the web UI the feasibility test proved works and needs no undiscovered
#: API.
DEFAULT_CATALOG_STRATEGY = CatalogStrategy.STRATEGY_B


@dataclass(frozen=True)
class LunaTitle:
    """One Luna title, as returned by a :class:`CatalogSource`.

    ``luna_product_id`` is the ``amzn1.adg.product.<UUID>`` identifier
    scraped from the browse-view tile ``id`` (``game_tile_<product_id>``).
    It is the **single required** id (spike-verified F6): the library DOM
    carries no slug and no ASIN. A title missing it is malformed and is
    skipped by the catalog writer (Req 4.7). The detail/launch URL is
    composed as ``{luna_host}/game/{luna_product_id}`` (see
    :func:`compose_detail_url`); Luna itself redirects that to the canonical
    slug+ASIN page. ``entitled`` records whether the account may play the
    title — best-effort from a Prime marker, which the spike saw on the
    detail page, not reliably on browse tiles (Req 3.7 caveat).
    """

    luna_product_id: str
    name: str
    entitled: bool


@dataclass(frozen=True)
class EntitlementSet:
    """The account's Luna entitlements plus the path that produced them.

    ``entitlements`` is the set of entitled title ids; **empty** means the
    account has no entitled titles, which the store maps to
    ``subscription_required`` (Req 3.3, 3.4). ``source`` records which
    acquisition path produced the data for diagnostics — ``"none"`` marks a
    set no path could produce (entitlement source unavailable, Req 3.6),
    kept distinct from an empty ``"api"``/``"dom-scrape"`` answer.
    """

    entitlements: tuple[str, ...]
    source: CatalogSourceKind


@runtime_checkable
class CatalogSource(Protocol):
    """Strategy boundary for Luna catalog/entitlement acquisition (Q4).

    A concrete source fetches the title list and the entitlement set from
    one path (API or DOM scrape). The two concrete implementations
    (Strategy A, Strategy B) are added by later tasks; this Protocol lets
    :class:`LunaCatalog` and the tests depend on the boundary rather than a
    specific path. ``@runtime_checkable`` so a fake can be asserted to
    satisfy the interface structurally in tests without inheritance.

    ``session`` is the active Luna CDP/browser session a source drives
    (Strategy B reads the library pages over it; a future Strategy A would
    carry its cookies); ``cfg`` supplies the endpoints and hosts.
    """

    async def fetch_titles(
        self, session: EdgeCDPClient, cfg: LunaConfig,
    ) -> list[LunaTitle]:
        """Fetch the account's Luna title list.

        An empty list means the account has zero titles (the store removes
        all Luna entries, Req 4.9); a transport/parse failure is signalled
        by raising, so the store can leave existing entries untouched and
        report a sync error (Req 4.2, 4.6) rather than mistaking a failure
        for "zero titles".
        """
        ...

    async def fetch_entitlements(
        self, session: EdgeCDPClient, cfg: LunaConfig,
    ) -> EntitlementSet:
        """Fetch the account's Luna entitlement set.

        The returned :class:`EntitlementSet` carries both the entitled ids
        and the ``source`` that produced them. An empty set with a real
        ``source`` is ``subscription_required`` (Req 3.4); a set with
        ``source="none"`` is ``entitlement_source_unavailable`` (Req 3.6).
        """
        ...
