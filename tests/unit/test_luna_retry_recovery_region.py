"""Example tests for Luna retry/recovery + region availability (task 11.2).

Drives :meth:`LunaStore.get_library` through the real retry/recovery loop
(:meth:`LunaStore._with_retry` → :meth:`LunaStore._sync_once`) and the real
region gate (:meth:`LunaStore._check_region`), mocking only the browser/CDP,
catalog-source, catalog-sync and region-probe seams — no real browser, no
Amazon. Mirrors the ``MicrosoftStore.get_library`` harness style
(``__new__`` + private-attr injection, a recording ``_Bus``), so the whole
store is drivable without a real ``bus``/``cache``/config.

Covers the four acceptance criteria of task 11.3:

* a failing mock catalog request retries exactly 3 times (4 attempts total)
  then surfaces (``get_library`` → ``None``), emitting a
  ``recovery_in_progress`` indicator while retrying (Req 11.5, 11.6);
* succeed-on-second-attempt stops early — no further retries (Req 11.5);
* region-unavailable (definitive within 5s) → ``get_library`` returns ``[]``,
  excluding every Luna title (Req 13.2);
* availability-unknown (inconclusive) → ``get_library`` returns ``None``,
  keeping the existing entries (Req 13.3).

_Requirements: 11.5, 11.6, 13.2, 13.3_
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.core.types import Events, Game
from unifideck.stores.luna.luna_catalog import (
    CatalogSyncResult,
    CatalogSyncStatus,
)
from unifideck.stores.luna.luna_store import (
    MAX_ATTEMPTS,
    MAX_RETRIES,
    LunaStatus,
    LunaStore,
)

# ── Test doubles ─────────────────────────────────────────────────────


class _Bus:
    """Records every emit so a test can read the status channel."""

    def __init__(self) -> None:
        self.emitted: list[tuple[Any, dict[str, Any]]] = []

    async def emit(self, event: Any, **payload: Any) -> None:
        self.emitted.append((event, payload))

    def skip_reasons(self) -> list[str]:
        return [
            p["reason"]
            for e, p in self.emitted
            if e == Events.SYNC_SKIPPED
        ]


class _Edge:
    """Stand-in for ``EdgeBrowser``: the browser is installed."""

    is_installed = True


class _Session:
    """Stand-in for ``LunaSessionMonitor``: a usable session exists."""

    def is_usable(self) -> bool:
        return True


class _CountingCatalog:
    """Fake ``LunaCatalog`` whose ``sync`` fails ``fail_times`` then succeeds.

    A failing sync is modelled as a :attr:`CatalogSyncStatus.SYNC_ERROR`
    result — exactly the transport/parse fault ``_sync_once`` turns into the
    recoverable raise the retry loop catches (Req 11.5). Records every call so
    the test can assert the exact attempt count.
    """

    def __init__(self, fail_times: int, games_on_success: list[Game]) -> None:
        self._fail_times = fail_times
        self._games = games_on_success
        self.calls = 0

    async def sync(
        self, session: Any, previous: Any,
    ) -> CatalogSyncResult:
        self.calls += 1
        if self.calls <= self._fail_times:
            return CatalogSyncResult(
                status=CatalogSyncStatus.SYNC_ERROR,
                games=None,
                error=f"mock catalog failure {self.calls}",
            )
        return CatalogSyncResult(
            status=CatalogSyncStatus.SYNCED,
            games=self._games,
        )


class _RegionSource:
    """Fake ``CatalogSource`` exposing only the ``region_unavailable`` probe.

    ``answer`` is what the probe returns: ``True`` (region cannot stream),
    ``False`` (available), or ``None`` (page could not decide → availability
    unknown). ``raises`` lets a test simulate an inconclusive probe fault.
    """

    def __init__(
        self, *, answer: bool | None = False, raises: Exception | None = None,
    ) -> None:
        self._answer = answer
        self._raises = raises
        self.calls = 0

    async def region_unavailable(self, cdp: Any, cfg: Any) -> bool | None:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._answer


_CDP_SENTINEL = object()


def _game(title_id: str) -> Game:
    """A minimal Luna browser-game ``Game`` for the catalog-success path."""
    return Game(
        app_id=1, store="luna", store_game_id=title_id, title=title_id,
    )


def _store(
    *,
    catalog: Any = None,
    catalog_source: Any = None,
    monkeypatch: pytest.MonkeyPatch,
    open_browser: bool = True,
) -> tuple[LunaStore, _Bus]:
    """Build a ``LunaStore`` wired to the fakes, bypassing ``__init__``.

    Only the attributes ``get_library`` → ``_with_retry`` → ``_sync_once`` →
    ``_check_region`` actually touch are set; the browser/CDP open+close and
    the previous-entries read are stubbed so no real I/O happens. The retry
    backoff is zeroed so the four-attempt path runs instantly.
    """
    store = LunaStore.__new__(LunaStore)
    store._edge = _Edge()
    store._session = _Session()
    store._bus = _Bus()
    store._catalog = catalog
    store._catalog_source = catalog_source or _RegionSource(answer=False)
    store._luna_config = object()
    store._last_stable_status = LunaStatus.NOT_CONNECTED

    # Stub the browser-open/close + previous-entries seams: the retry/region
    # logic under test must run, but spawning a kiosk / reading the cache must
    # not. ``_open_browser_for_sync`` yields the live-CDP sentinel the fakes
    # ignore, or ``None`` to drive the "could not open" recoverable failure.
    async def _open() -> Any:
        return _CDP_SENTINEL if open_browser else None

    async def _close() -> None:
        return None

    monkeypatch.setattr(store, "_open_browser_for_sync", _open)
    monkeypatch.setattr(store, "_close_sync_browser", _close)
    monkeypatch.setattr(store, "_previous_luna_entries", list)
    # Keep the retry budget instant.
    monkeypatch.setattr(
        "unifideck.stores.luna.luna_store._RETRY_BACKOFF_SECONDS", 0.0,
    )
    return store, store._bus


# ── Retry / recovery (Req 11.5, 11.6) ────────────────────────────────


async def test_failing_request_retries_exactly_three_times_then_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistently failing catalog request retries 3× then surfaces None.

    Four attempts total (first try + 3 retries, Req 11.5); ``get_library``
    returns ``None`` so the existing Luna shortcuts are kept (not an empty,
    authoritative library).
    """
    catalog = _CountingCatalog(fail_times=MAX_ATTEMPTS, games_on_success=[])
    store, _bus = _store(catalog=catalog, monkeypatch=monkeypatch)

    result = await store.get_library()

    assert result is None
    assert catalog.calls == MAX_ATTEMPTS == 4


async def test_retry_emits_recovery_in_progress_while_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While retrying, a ``recovery_in_progress`` indicator is emitted.

    One indicator per retry (3 for a fully-failing request), and the last
    stable state is restored at the end so the panel offers a retry action
    (Req 11.1, 11.6).
    """
    catalog = _CountingCatalog(fail_times=MAX_ATTEMPTS, games_on_success=[])
    store, bus = _store(catalog=catalog, monkeypatch=monkeypatch)

    await store.get_library()

    reasons = bus.skip_reasons()
    assert reasons.count(LunaStatus.RECOVERY_IN_PROGRESS) == MAX_RETRIES == 3
    # The retry helper restores the last stable state before surfacing.
    assert reasons[-1] == LunaStatus.NOT_CONNECTED


async def test_succeed_on_second_attempt_stops_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request that fails once then succeeds does not retry further.

    Exactly two attempts (one failure, one success); the catalog answer is
    passed straight through, and only one recovery indicator is emitted (for
    the single retry), with no stable-state restore (nothing failed terminally).
    """
    games = [_game("amzn1.adg.product.A")]
    catalog = _CountingCatalog(fail_times=1, games_on_success=games)
    store, bus = _store(catalog=catalog, monkeypatch=monkeypatch)

    result = await store.get_library()

    assert result == games
    assert catalog.calls == 2
    assert bus.skip_reasons().count(LunaStatus.RECOVERY_IN_PROGRESS) == 1


# ── Region availability (Req 13.2, 13.3) ──────────────────────────────


async def test_region_unavailable_excludes_all_titles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A definitive region-unavailable answer returns the empty library.

    ``[]`` is authoritative — the sync layer drops every Luna shortcut — so a
    region that cannot stream excludes all titles (Req 13.2). The catalog sync
    is never reached, and ``region_unavailable`` is surfaced on the status
    channel.
    """
    # A catalog that would blow up if called proves the region gate
    # short-circuits before any catalog work.
    class _Exploding:
        async def sync(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("catalog must not run when region unavailable")

    source = _RegionSource(answer=True)
    store, bus = _store(
        catalog=_Exploding(), catalog_source=source, monkeypatch=monkeypatch,
    )

    result = await store.get_library()

    assert result == []
    assert source.calls == 1
    assert LunaStatus.REGION_UNAVAILABLE in bus.skip_reasons()


async def test_availability_unknown_keeps_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inconclusive region check keeps the existing entries (returns None).

    A probe that cannot decide (returns ``None``) is availability-unknown, so
    ``get_library`` returns ``None`` and the previously synchronized Luna
    entries are left unchanged (Req 13.3) — never mistaken for an empty library.
    """
    source = _RegionSource(answer=None)
    store, _bus = _store(catalog=None, catalog_source=source, monkeypatch=monkeypatch)

    result = await store.get_library()

    assert result is None
    assert store._last_stable_status == LunaStatus.AVAILABILITY_UNKNOWN


async def test_availability_unknown_on_probe_fault_keeps_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A region probe that faults is treated as availability-unknown.

    Any probe fault (timeout, unreadable, error) within the 10s ceiling is
    inconclusive, so the entries are kept (``None``) rather than dropped
    (Req 13.3).
    """
    source = _RegionSource(raises=RuntimeError("probe blew up"))
    store, _bus = _store(catalog=None, catalog_source=source, monkeypatch=monkeypatch)

    result = await store.get_library()

    assert result is None
    assert store._last_stable_status == LunaStatus.AVAILABILITY_UNKNOWN
