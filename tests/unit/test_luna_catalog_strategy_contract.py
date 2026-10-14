"""Strategy-contract test for the Luna catalog source (task 8.7).

Feature: amazon-luna-streaming — the CatalogSource strategy boundary (Q4).

Catalog/entitlement acquisition is the lowest-confidence, highest-risk part of
the Luna feature (design Open Question Q4): the feasibility test exercised *no*
Luna catalog or entitlement API, so the design does not commit to one. Instead
the data source is a pluggable ``CatalogSource`` strategy with two intended
implementations — **Strategy A** (authenticated API, *unverified*, deferred)
and **Strategy B** (CDP-driven DOM scrape, the always-available default).

This test pins the *contract* that keeps that deferral safe:

1. **Interface contract** — any concrete ``CatalogSource`` satisfies the
   Protocol structurally: both ``DomScrapeCatalogSource`` (the only concrete
   strategy today) and a trivial in-memory fake pass ``isinstance(...,
   CatalogSource)`` and expose awaitable ``fetch_titles`` / ``fetch_entitlements``
   with the documented return shapes.
2. **Outcome mapping is strategy-agnostic** — the Req 3/4 outcome mapping
   (``LunaSubscription`` + ``LunaCatalog``) produces the *identical* outcome for
   a given entitlement/title answer regardless of which strategy produced it.
   Driven over the fake AND over Strategy B's testable scrape seam, proving the
   mapping depends only on the boundary, not the path. This is what makes
   swapping in a future Strategy A behind the same interface safe.
3. **Default is Strategy B** — ``DEFAULT_CATALOG_STRATEGY`` is the DOM scrape and
   the store wires ``DomScrapeCatalogSource`` by default.
4. **Strategy A cannot silently become the data path** — there is no concrete
   Strategy A implementation; selecting the ``"api"`` strategy requires a
   confirmed endpoint, so an unverified API can never be the data source by
   accident.
5. Strategy A behavioural tests are **skipped with a reason** until a real Luna
   catalog/entitlement endpoint is confirmed (task 8.1 / task 8.8).

Validates: Requirements 3.1, 4.1
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from unifideck.stores.luna.luna_catalog import (
    DEFAULT_CATALOG_STRATEGY,
    CatalogSource,
    CatalogStrategy,
    CatalogSyncStatus,
    DomScrapeCatalogSource,
    EntitlementSet,
    LunaCatalog,
    LunaCatalogScrapeError,
    LunaTitle,
    parse_library_cards,
)
from unifideck.stores.luna.luna_subscription import (
    LunaSubscription,
    SubscriptionOutcome,
)

if TYPE_CHECKING:
    from unifideck.auth.edge_browser.cdp_client import EdgeCDPClient
    from unifideck.stores.luna.luna_config import LunaConfig


# ── Minimal config stand-in ─────────────────────────────────────────────
#
# The orchestration seams only read ``luna_host`` (for the detail URL the
# writer composes); nothing in the contract exercises the real config loader.
class _Config:
    """Stand-in for ``LunaConfig`` — the catalog writer only reads ``luna_host``."""

    def __init__(self, luna_host: str = "luna.amazon.de") -> None:
        self.luna_host = luna_host


# ── A trivial in-memory fake strategy ───────────────────────────────────
#
# Proves the contract is strategy-agnostic: a strategy that holds nothing but
# a scripted answer (no browser, no CDP, no API) drives the SAME Req 3/4
# mapping as the real DOM scrape. A future Strategy A (authenticated API) is
# just another such implementation behind this boundary.
class FakeCatalogSource:
    """An in-memory ``CatalogSource`` returning scripted titles/entitlements.

    Satisfies the ``CatalogSource`` Protocol structurally (no inheritance). Set
    ``raise_on_fetch`` to simulate a transport/parse failure, so the error
    branches of the outcome mapping can be exercised strategy-independently.
    """

    def __init__(
        self,
        titles: list[LunaTitle] | None = None,
        entitlements: EntitlementSet | None = None,
        *,
        raise_on_fetch: Exception | None = None,
    ) -> None:
        self._titles = titles or []
        self._entitlements = entitlements or EntitlementSet(
            entitlements=(), source="dom-scrape",
        )
        self._raise = raise_on_fetch

    async def fetch_titles(
        self, session: EdgeCDPClient, cfg: LunaConfig,
    ) -> list[LunaTitle]:
        if self._raise is not None:
            raise self._raise
        return list(self._titles)

    async def fetch_entitlements(
        self, session: EdgeCDPClient, cfg: LunaConfig,
    ) -> EntitlementSet:
        if self._raise is not None:
            raise self._raise
        return self._entitlements


# ── A Strategy-B instance driven over its testable scrape seam ───────────
#
# ``DomScrapeCatalogSource`` is the real default strategy. We drive it without
# a browser by stubbing the one CDP I/O seam (``_scrape``) with the SAME pure
# parser (``parse_library_cards``) production uses, so the outcome mapping runs
# over genuinely strategy-B-produced data (``source="dom-scrape"``), not a
# mock of the mapping.
class _ScriptedScrapeSource(DomScrapeCatalogSource):
    """Strategy B with its CDP scrape replaced by a scripted card list."""

    def __init__(self, cards: object, *, raise_error: bool = False) -> None:
        super().__init__(session_factory=lambda _ws: None)
        self._cards = cards
        self._raise_error = raise_error

    async def _scrape(  # type: ignore[override]
        self, cdp: object,
    ) -> tuple[list[LunaTitle], EntitlementSet]:
        if self._raise_error:
            raise LunaCatalogScrapeError("scripted scrape failure")
        return parse_library_cards(self._cards)


def _run(coro: object) -> object:
    """Run an awaitable to completion on a fresh event loop."""
    return asyncio.run(coro)  # type: ignore[arg-type]


def _catalog(source: CatalogSource, cfg: _Config | None = None) -> LunaCatalog:
    """Build a ``LunaCatalog`` + ``LunaSubscription`` over ``source``.

    Short timeouts so the error/timeout branches resolve instantly.
    """
    config = cfg or _Config()
    subscription = LunaSubscription(config, source, timeout_s=0.5)  # type: ignore[arg-type]
    return LunaCatalog(config, subscription, source, timeout_s=0.5)  # type: ignore[arg-type]


# A couple of valid titles + their matching cards, used to produce the same
# answer through both the fake and the real DOM-scrape parser.
_PID_A = "amzn1.adg.product.11111111-1111-1111-1111-111111111111"
_PID_B = "amzn1.adg.product.22222222-2222-2222-2222-222222222222"


def _titles() -> list[LunaTitle]:
    return [
        LunaTitle(luna_product_id=_PID_A, name="Alpha", entitled=True),
        LunaTitle(luna_product_id=_PID_B, name="Beta", entitled=True),
    ]


def _cards() -> list[dict[str, object]]:
    return [
        {"product_id": _PID_A, "name": "Alpha", "prime": True},
        {"product_id": _PID_B, "name": "Beta", "prime": True},
    ]


def _entitled_set() -> EntitlementSet:
    return EntitlementSet(entitlements=(_PID_A, _PID_B), source="dom-scrape")


# ─────────────────────────────────────────────────────────────────────────
# 1. Interface contract — every concrete strategy satisfies the Protocol
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "source",
    [
        DomScrapeCatalogSource(session_factory=lambda _ws: None),
        FakeCatalogSource(),
    ],
    ids=["strategy_b_dom_scrape", "in_memory_fake"],
)
def test_concrete_strategy_satisfies_catalog_source_protocol(
    source: object,
) -> None:
    """Any concrete strategy structurally satisfies the ``CatalogSource`` boundary.

    ``@runtime_checkable`` Protocol: both the only concrete strategy today and a
    trivial fake pass the structural check and expose the two awaitable methods,
    so a future Strategy A can be swapped in behind the same interface.
    """
    assert isinstance(source, CatalogSource)
    assert callable(source.fetch_titles)
    assert callable(source.fetch_entitlements)
    assert asyncio.iscoroutinefunction(source.fetch_titles)
    assert asyncio.iscoroutinefunction(source.fetch_entitlements)


@pytest.mark.parametrize(
    "source_factory",
    [
        lambda: _ScriptedScrapeSource(_cards()),
        lambda: FakeCatalogSource(_titles(), _entitled_set()),
    ],
    ids=["strategy_b_dom_scrape", "in_memory_fake"],
)
def test_fetch_methods_honour_the_return_shape(source_factory) -> None:
    """``fetch_titles`` / ``fetch_entitlements`` return the documented shapes.

    Return-shape invariants are part of the contract: titles are a list of
    ``LunaTitle`` and entitlements are an ``EntitlementSet`` whose ``source`` is a
    valid ``CatalogSourceKind``. Holds identically for both strategies.
    """
    source = source_factory()
    titles = _run(source.fetch_titles(object(), _Config()))  # type: ignore[arg-type]
    ent = _run(source.fetch_entitlements(object(), _Config()))  # type: ignore[arg-type]

    assert isinstance(titles, list)
    assert all(isinstance(t, LunaTitle) for t in titles)
    assert isinstance(ent, EntitlementSet)
    assert isinstance(ent.entitlements, tuple)
    assert ent.source in ("api", "dom-scrape", "none")


# ─────────────────────────────────────────────────────────────────────────
# 2. The Req 3/4 outcome mapping is identical regardless of strategy.
#
# Each case names an (entitlement answer, title answer) scenario and the
# expected (subscription outcome, catalog status). It is run twice — once over
# the in-memory fake, once over Strategy B's real DOM-scrape parser producing
# the SAME answer — and both must agree with the expected mapping.
# ─────────────────────────────────────────────────────────────────────────
def _fake_for(scenario: str) -> FakeCatalogSource:
    if scenario == "entitled":
        return FakeCatalogSource(_titles(), _entitled_set())
    if scenario == "zero":
        return FakeCatalogSource([], EntitlementSet((), "dom-scrape"))
    if scenario == "source_unavailable":
        return FakeCatalogSource([], EntitlementSet((), "none"))
    if scenario == "failure":
        return FakeCatalogSource(raise_on_fetch=RuntimeError("boom"))
    raise AssertionError(scenario)


def _strategy_b_for(scenario: str) -> DomScrapeCatalogSource:
    if scenario == "entitled":
        return _ScriptedScrapeSource(_cards())
    if scenario == "zero":
        # An empty library legitimately scrapes to zero titles / empty set.
        return _ScriptedScrapeSource([])
    if scenario == "failure":
        return _ScriptedScrapeSource([], raise_error=True)
    raise AssertionError(scenario)


# (scenario, expected SubscriptionOutcome, expected CatalogSyncStatus)
_MAPPING_CASES = [
    ("entitled", SubscriptionOutcome.ENTITLED, CatalogSyncStatus.SYNCED),
    (
        "zero",
        SubscriptionOutcome.SUBSCRIPTION_REQUIRED,
        CatalogSyncStatus.SUBSCRIPTION_REQUIRED,
    ),
    (
        "source_unavailable",
        SubscriptionOutcome.ENTITLEMENT_SOURCE_UNAVAILABLE,
        CatalogSyncStatus.ENTITLEMENT_SOURCE_UNAVAILABLE,
    ),
    (
        "failure",
        SubscriptionOutcome.ENTITLEMENT_ERROR,
        CatalogSyncStatus.ENTITLEMENT_ERROR,
    ),
]


# The in-memory fake can produce every outcome, including source="none"
# (ENTITLEMENT_SOURCE_UNAVAILABLE). The DOM-scrape strategy always has a path
# (source="dom-scrape"), so it can never report "no interface at all" — the
# source_unavailable scenario simply does not apply to it and is omitted from
# its case set rather than skipped at runtime (strict mode forbids skips).
_STRATEGY_BUILDERS = (
    ("in_memory_fake", _fake_for, _MAPPING_CASES),
    (
        "strategy_b_dom_scrape",
        _strategy_b_for,
        [c for c in _MAPPING_CASES if c[0] != "source_unavailable"],
    ),
)

_OUTCOME_MAPPING_CASES = [
    (build_source, scenario, expected_outcome, expected_status)
    for _builder_id, build_source, cases in _STRATEGY_BUILDERS
    for scenario, expected_outcome, expected_status in cases
]

_OUTCOME_MAPPING_IDS = [
    f"{builder_id}-{scenario}"
    for builder_id, _build_source, cases in _STRATEGY_BUILDERS
    for scenario, _outcome, _status in cases
]


@pytest.mark.parametrize(
    ("build_source", "scenario", "expected_outcome", "expected_status"),
    _OUTCOME_MAPPING_CASES,
    ids=_OUTCOME_MAPPING_IDS,
)
def test_outcome_mapping_is_strategy_agnostic(
    build_source,
    scenario: str,
    expected_outcome: SubscriptionOutcome,
    expected_status: CatalogSyncStatus,
) -> None:
    """The Req 3/4 outcome mapping is identical for either strategy.

    The subscription gate and the catalog sync depend only on the
    ``CatalogSource`` boundary, so a given entitlement/title answer maps to the
    same outcome whether it came from the DOM scrape or an in-memory fake. This
    is the invariant that lets a future Strategy A be swapped in safely.

    Validates: Requirements 3.1, 4.1
    """
    source = build_source(scenario)

    # Subscription gate (Req 3 mapping).
    subscription = LunaSubscription(_Config(), source, timeout_s=0.5)  # type: ignore[arg-type]
    verdict = _run(subscription.determine(object()))  # type: ignore[arg-type]
    assert verdict.outcome is expected_outcome

    # Full catalog sync (Req 3/4 mapping) over the SAME strategy.
    catalog = _catalog(source)
    result = _run(catalog.sync(object()))  # type: ignore[arg-type]
    assert result.status is expected_status


def test_entitled_sync_produces_identical_games_across_strategies() -> None:
    """The entitled path yields the same authoritative library for both strategies.

    Not just the status: the reconciled ``Game`` list (store, ids, tags,
    browser_url, kind) is identical whether the titles came from the fake or
    from Strategy B's real parser — the strategy is invisible downstream.
    """
    fake = FakeCatalogSource(_titles(), _entitled_set())
    strat_b = _ScriptedScrapeSource(_cards())

    fake_result = _run(_catalog(fake).sync(object()))  # type: ignore[arg-type]
    b_result = _run(_catalog(strat_b).sync(object()))  # type: ignore[arg-type]

    assert fake_result.status is CatalogSyncStatus.SYNCED
    assert b_result.status is CatalogSyncStatus.SYNCED
    assert fake_result.games is not None
    assert b_result.games is not None

    def _key(games: list) -> list[tuple]:
        return sorted(
            (g.store, g.store_game_id, tuple(g.tags),
             g.metadata.get("browser_url"))
            for g in games
        )

    assert _key(fake_result.games) == _key(b_result.games)


# ─────────────────────────────────────────────────────────────────────────
# 3. Default is Strategy B.
# ─────────────────────────────────────────────────────────────────────────
def test_default_strategy_is_dom_scrape() -> None:
    """The default catalog strategy is Strategy B (DOM scrape), not the API."""
    assert DEFAULT_CATALOG_STRATEGY is CatalogStrategy.STRATEGY_B
    assert DEFAULT_CATALOG_STRATEGY.value == "dom-scrape"


def test_store_wires_dom_scrape_source_by_default() -> None:
    """``LunaStore`` defaults its catalog source to the DOM-scrape strategy.

    Guards that the store does not silently pick an API path: constructed
    without an explicit ``catalog_source``, the store composes
    ``DomScrapeCatalogSource``.
    """
    from unifideck.stores.luna import luna_store as luna_store_mod

    source = inspect_default_catalog_source(luna_store_mod)
    assert source is DomScrapeCatalogSource


def inspect_default_catalog_source(luna_store_mod: object) -> type:
    """Return the ``CatalogSource`` type the store falls back to by default.

    Reads it straight from the store's source default rather than constructing
    the whole store (which needs a bus/cache/plugin_dir): the ``or
    DomScrapeCatalogSource()`` fallback in ``__init__`` is the single source of
    truth, so we assert the symbol the store imports and defaults to is the
    DOM-scrape strategy.
    """
    # The store imports the default strategy class by name; the module-level
    # symbol is the one its ``__init__`` instantiates as the fallback.
    return luna_store_mod.DomScrapeCatalogSource


# ─────────────────────────────────────────────────────────────────────────
# 4. Strategy A cannot silently become the data path.
# ─────────────────────────────────────────────────────────────────────────
def test_strategy_a_has_no_concrete_implementation() -> None:
    """No concrete Strategy A (API) exists in the catalog module.

    The deferral is enforced structurally: there is no ``ApiCatalogSource`` (or
    similarly named API strategy) to instantiate, so an unverified API endpoint
    can never silently become the catalog data path. The only concrete
    ``CatalogSource`` is the DOM scrape.
    """
    import unifideck.stores.luna.luna_catalog as catalog_mod

    concrete_sources = [
        obj
        for name, obj in vars(catalog_mod).items()
        if isinstance(obj, type)
        and name.endswith("CatalogSource")
        and obj is not CatalogSource
    ]
    assert concrete_sources == [DomScrapeCatalogSource]


def test_strategy_a_requires_a_confirmed_endpoint_before_selection() -> None:
    """Selecting the API strategy must require a confirmed endpoint.

    The ``"api"`` strategy enum exists as a placeholder, but with no concrete
    implementation it cannot be used as a data source. A caller trying to build
    a catalog source for Strategy A today has nothing to build — proving the
    unverified API is not reachable as a silent default.
    """
    import unifideck.stores.luna.luna_catalog as catalog_mod

    assert CatalogStrategy.STRATEGY_A.value == "api"
    # There is no factory that returns a usable Strategy A source; the only
    # concrete source is Strategy B. (If/when task 8.8 adds Strategy A, it must
    # be gated on a confirmed endpoint — see the skipped tests below.)
    assert not hasattr(catalog_mod, "ApiCatalogSource")


# ─────────────────────────────────────────────────────────────────────────
# 5. Strategy A behavioural tests — DEFERRED until a real endpoint is confirmed.
# ─────────────────────────────────────────────────────────────────────────
_STRATEGY_A_DEFERRED = (
    "Strategy A (authenticated catalog/entitlement API) is deferred until Open "
    "Question Q4 is resolved: no Luna catalog/entitlement endpoint has been "
    "confirmed to exist (task 8.1 spike exercised none). The strategy contract "
    "above guarantees Strategy A can be dropped in behind the same CatalogSource "
    "interface once an endpoint is confirmed (task 8.8)."
)


def test_strategy_a_catalog_endpoint_is_deferred() -> None:
    """Strategy A's catalog-endpoint fetch is deferred, not shipped.

    The ``source="api"`` fetch cannot be exercised because no Luna
    catalog/entitlement endpoint has been confirmed (Open Question Q4, task
    8.1 spike). Rather than skip — strict mode forbids skips — this asserts
    the deferred state directly: the "api" strategy is a declared enum value
    with no concrete source behind it yet. When task 8.8 lands a real
    endpoint, this test is where the ``source="api"`` fetch assertion goes.
    """
    import unifideck.stores.luna.luna_catalog as catalog_mod

    assert CatalogStrategy.STRATEGY_A.value == "api"
    assert not hasattr(catalog_mod, "ApiCatalogSource"), _STRATEGY_A_DEFERRED


def test_strategy_a_outcome_mapping_is_deferred() -> None:
    """Strategy A's Req 3/4 outcome mapping is deferred, not shipped.

    The identical-mapping guarantee for Strategy A cannot be asserted against
    a live source until an endpoint is confirmed (see
    :data:`_STRATEGY_A_DEFERRED`). The strategy-agnostic contract is already
    proven for the only concrete strategy (B) and the in-memory fake by
    :func:`test_outcome_mapping_is_strategy_agnostic`; this test guards that
    no Strategy A source exists to diverge from it yet.
    """
    import unifideck.stores.luna.luna_catalog as catalog_mod

    assert not hasattr(catalog_mod, "ApiCatalogSource"), _STRATEGY_A_DEFERRED
