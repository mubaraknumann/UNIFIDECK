"""Property test for the Luna catalog sync reconciliation (task 8.6).

``LunaCatalog.sync()`` turns a retrieved catalog list into the authoritative
value the sync layer writes back into ``library_cache.json``. The
reconciliation contract (``LunaCatalog._reconcile``) is the invariant worth
pinning across arbitrary (previous-entries, retrieved-titles) pairs:

* a successfully retrieved ``list[LunaTitle]`` is **authoritative** — a
  previously-synced title that is no longer present drops out simply by being
  absent (Req 4.8);
* a retrieval of **zero** titles removes every Luna entry (``games == []``,
  Req 4.9);
* a **malformed** title (missing ``luna_product_id``) is skipped, and its
  pre-existing entry is **preserved unchanged** rather than dropped by the
  authoritative replace (Req 4.7);
* a sync **failure** (fetch raised / timed out) leaves the previous entries
  untouched (``games is None``, Req 4.2/4.6) — the ``None`` branch under test
  as the complement of the authoritative replace.

The test drives the REAL ``LunaCatalog.sync()`` + ``_reconcile`` over a mocked
``CatalogSource`` and ``LunaSubscription`` so no browser / Amazon / CDP is
touched. ``previous_entries`` are serialized into a temp ``library_cache.json``
exactly the way the sync layer persists them (``asdict`` through a JSON
round-trip) and read back, so the writer sees the same bytes production would.

The invariant under test (design **Property 6**, Req 4.7/4.8/4.9/3.3):

    for any previously synchronized set of Luna entries and any successfully
    retrieved catalog list, after sync the set of Luna entry ids equals the set
    of valid retrieved title ids — titles no longer present are removed, zero
    valid titles leaves no Luna entries, and malformed titles are skipped with
    their pre-existing entries preserved unchanged.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from unifideck.launcher.browser_games import BROWSER_URL_KEY
from unifideck.stores.luna.luna_catalog import (
    LUNA_STORE_TAG,
    CatalogSyncStatus,
    EntitlementSet,
    LunaCatalog,
    LunaTitle,
    compose_detail_url,
)
from unifideck.stores.luna.luna_subscription import (
    SubscriptionOutcome,
    SubscriptionResult,
)

LUNA_HOST = "https://luna.amazon.de"

# ── Generators constraining to the real catalog input space ──

# Luna's single required id is ``amzn1.adg.product.<UUID>`` (spike-verified
# F6). We vary the UUID tail over a small alphabet so collisions between the
# "previous" and "fetched" id sets happen often (exercising the overlap /
# removal / carry-forward paths), while staying structurally valid.
_id_tails = st.text("0123456789abcdef", min_size=1, max_size=6)
_product_ids = _id_tails.map(lambda t: f"amzn1.adg.product.{t}")
_names = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")),
    min_size=0,
    max_size=30,
)


def _valid_title(product_id: str, name: str) -> LunaTitle:
    return LunaTitle(luna_product_id=product_id, name=name, entitled=True)


def _malformed_title(name: str) -> LunaTitle:
    # Missing id ⇒ the reconciler treats it as malformed and skips it.
    return LunaTitle(luna_product_id="", name=name, entitled=True)


# A fetched catalog list: a mix of valid titles (keyed by id) and malformed
# (id-less) titles, in arbitrary order.
# Valid titles are NOT constrained to unique ids: the DOM scraper (Strategy B)
# dedupes via its ``seen`` set, but the ``CatalogSource`` contract must hold for
# any strategy — a future API strategy (Strategy A) could return the same
# product id twice. Allowing duplicates here exercises the reconciler's own
# dedup so it cannot emit two library entries with the same ``store_game_id``.
_valid_fetched = st.lists(
    st.tuples(_product_ids, _names),
    max_size=6,
).map(lambda pairs: [_valid_title(pid, nm) for pid, nm in pairs])
_malformed_fetched = st.lists(_names, max_size=3).map(
    lambda names: [_malformed_title(nm) for nm in names],
)
_fetched_titles = st.builds(
    lambda good, bad: good + bad, _valid_fetched, _malformed_fetched,
).map(lambda ts: ts)  # keep as-is; order variety comes from the lists above

# Previously-synced Luna ids already on disk. Independent of the fetched set
# so overlap, strict-superset (removals), and disjoint (full carry-forward on
# malformed) cases all arise.
_previous_ids = st.lists(_product_ids, max_size=6, unique=True)


# ── Mocks: no browser / Amazon / CDP ──


class _FakeSource:
    """A ``CatalogSource`` returning a scripted title list (no I/O)."""

    def __init__(self, titles: list[LunaTitle]) -> None:
        self._titles = titles

    async def fetch_titles(self, session: Any, cfg: Any) -> list[LunaTitle]:
        return list(self._titles)

    async def fetch_entitlements(self, session: Any, cfg: Any) -> EntitlementSet:
        return EntitlementSet(entitlements=(), source="dom-scrape")


class _FakeSubscription:
    """A ``LunaSubscription`` stand-in with a scripted outcome (no I/O)."""

    def __init__(self, outcome: SubscriptionOutcome) -> None:
        self._outcome = outcome

    async def determine(self, session: Any) -> SubscriptionResult:
        return SubscriptionResult(
            outcome=self._outcome,
            entitlements=EntitlementSet(entitlements=(), source="dom-scrape"),
        )


class _Config:
    """Minimal ``LunaConfig`` stand-in — the writer only reads ``luna_host``."""

    luna_host = LUNA_HOST


def _serialize_previous(ids: list[str], tmp_path: Path) -> list[dict[str, Any]]:
    """Write ``ids`` as Luna entries to a temp ``library_cache.json`` and read
    them back as the serialized dicts the store hands to ``sync()``.

    Mirrors the sync layer's persistence (the serialized ``Game`` dict through
    a JSON round-trip), so ``_reconcile`` sees exactly the on-disk shape the
    store reads back and hands to ``sync()``.
    """
    entries: list[dict[str, Any]] = []
    for pid in ids:
        entries.append({
            "app_id": 0,
            "store": "luna",
            "store_game_id": pid,
            "title": pid,
            "installed": False,
            "tags": ["browser", LUNA_STORE_TAG],
            "metadata": {BROWSER_URL_KEY: compose_detail_url(LUNA_HOST, pid)},
        })
    cache_path = tmp_path / "library_cache.json"
    payload = {"last_sync_time": None, "libraries": {"luna": entries}}
    cache_path.write_text(json.dumps(payload), encoding="utf-8")
    # Read back exactly what the store would pass into sync().
    loaded = json.loads(cache_path.read_text(encoding="utf-8"))
    return list(loaded["libraries"]["luna"])


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    deadline=None,
)
@given(fetched=_fetched_titles, previous_ids=_previous_ids)
def test_luna_catalog_sync_reconciliation(
    fetched: list[LunaTitle],
    previous_ids: list[str],
    tmp_path: Path,
) -> None:
    """Feature: amazon-luna-streaming, Property 6: Catalog sync reconciliation.

    For any previously synchronized set of Luna browser-game library entries and
    any successfully retrieved catalog list, after sync the set of Luna entry
    ids equals the set of valid retrieved title ids: titles no longer present
    are removed, zero valid titles leaves no Luna entries, and malformed titles
    (missing ``luna_product_id``) are skipped with their pre-existing entries
    preserved unchanged. The reconciled set holds exactly one entry per product
    id even when the fetched list repeats an id — a duplicate id in the fetched
    catalog never produces a duplicate library entry.

    Validates: Requirements 4.7, 4.8, 4.9, 3.3
    """
    previous_entries = _serialize_previous(previous_ids, tmp_path)
    previous_id_set = set(previous_ids)

    valid_ids = {t.luna_product_id for t in fetched if t.luna_product_id}
    has_malformed = any(not t.luna_product_id for t in fetched)

    catalog = LunaCatalog(
        config=_Config(),  # type: ignore[arg-type]
        subscription=_FakeSubscription(SubscriptionOutcome.ENTITLED),  # type: ignore[arg-type]
        source=_FakeSource(fetched),  # type: ignore[arg-type]
    )

    result = _run(catalog.sync(session=None, previous_entries=previous_entries))

    # A successful retrieval always yields an authoritative list (never None).
    assert result.games is not None
    synced_ids = [g.store_game_id for g in result.games]

    # Exactly one entry per product id — a repeated id in the fetched catalog
    # must NOT yield a duplicate library entry (the reconciler dedups on
    # ``luna_product_id`` so the contract holds for any CatalogSource).
    assert len(synced_ids) == len(set(synced_ids))
    synced_id_set = set(synced_ids)
    # len(synced) == |distinct valid ids ∪ carried-forward ids|: no duplicates,
    # nothing fabricated. (carried-forward only on malformed; empty otherwise.)
    carried_ids = (previous_id_set - valid_ids) if has_malformed else set()
    assert len(synced_ids) == len(valid_ids | carried_ids)

    # Every synced entry is a valid Luna browser-game entry.
    for game in result.games:
        assert game.store == "luna"
        assert "browser" in game.tags
        assert LUNA_STORE_TAG in game.tags
        assert game.store_game_id
        assert game.metadata[BROWSER_URL_KEY] == compose_detail_url(
            LUNA_HOST, game.store_game_id,
        )

    if not fetched:
        # Zero titles retrieved: authoritative empty list removes everything.
        assert result.status is CatalogSyncStatus.SYNCED_ZERO
        assert result.games == []
        return

    # Every valid retrieved id is present in the synced set (Req 4.8: the
    # retrieved list is authoritative; titles no longer present are absent).
    assert valid_ids <= synced_id_set

    if has_malformed:
        assert result.status is CatalogSyncStatus.SYNCED_WITH_MALFORMED
        # Pre-existing entries for ids not produced this run are carried
        # forward unchanged (Req 4.7) — never dropped by the malformed skip.
        preserved = previous_id_set - valid_ids
        assert preserved <= synced_id_set
        # The synced set is exactly the valid ids plus the carried-forward
        # pre-existing ids — no stray / fabricated ids.
        assert synced_id_set == valid_ids | preserved
    else:
        # No malformed titles: the synced set is EXACTLY the valid retrieved
        # ids — titles no longer present are removed (Req 4.8), carry-forward
        # is not triggered.
        assert result.status is CatalogSyncStatus.SYNCED
        assert synced_id_set == valid_ids


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    deadline=None,
)
@given(previous_ids=_previous_ids)
def test_luna_catalog_sync_error_leaves_previous_unchanged(
    previous_ids: list[str],
    tmp_path: Path,
) -> None:
    """Feature: amazon-luna-streaming, Property 6: Catalog sync reconciliation.

    A sync-error outcome (the catalog fetch raised) leaves the previously
    synchronized Luna entries unchanged: ``sync`` returns ``games is None`` so
    the sync layer keeps the existing ``library_cache.json`` entries, the
    complement of the authoritative-replace path above.

    Validates: Requirements 4.7, 4.8, 4.9, 3.3
    """
    previous_entries = _serialize_previous(previous_ids, tmp_path)

    class _RaisingSource(_FakeSource):
        async def fetch_titles(self, session: Any, cfg: Any) -> list[LunaTitle]:
            raise RuntimeError("scrape failed")

    catalog = LunaCatalog(
        config=_Config(),  # type: ignore[arg-type]
        subscription=_FakeSubscription(SubscriptionOutcome.ENTITLED),  # type: ignore[arg-type]
        source=_RaisingSource([]),  # type: ignore[arg-type]
    )

    result = _run(catalog.sync(session=None, previous_entries=previous_entries))

    assert result.status is CatalogSyncStatus.SYNC_ERROR
    assert result.games is None  # previous entries left untouched
