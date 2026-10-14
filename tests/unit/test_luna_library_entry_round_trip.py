"""Property test for the Luna browser-game library-entry round-trip (task 7.2).

A Luna title is persisted as an ordinary **browser game of kind ``stream``**
in ``library_cache.json``: the catalog writer (``LunaCatalog._to_game``) tags
it ``browser`` + ``luna`` and sets ``metadata["browser_url"]`` to the
``{luna_host}/game/{product_id}`` detail URL; the launcher resolves it back
through ``browser_games.cached_game`` / ``browser_games.browser_target`` into a
``BrowserTarget{url, kind="stream"}``. These two halves live in different
modules (the store-side writer and the stdlib-only launcher reader) and only
share bytes on disk, so the invariant worth pinning is that a title survives
the trip through ``library_cache.json`` **losslessly**.

The invariant under test (design **Property 5**, Req 4.3/4.4/4.5):

    for any valid Luna title (product id, name), writing it as a browser-game
    entry and reading it back yields the same ``luna_product_id``, the same
    composed ``{luna_host}/game/{product_id}`` URL, kind ``stream``, and a host
    that passes ``is_allowed_browser_url``.

The test drives the REAL writer and the REAL launcher reader over a temp
``library_cache.json`` serialized exactly the way the sync layer serializes it
(``dataclasses.asdict`` through a JSON round-trip, which renders the
``GameTag`` StrEnum members as the plain ``"browser"`` / ``"luna"`` strings the
launcher sees). Product ids, names, and regional Luna hosts are all varied.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from unifideck.launcher import browser_games
from unifideck.launcher.browser_games import (
    BrowserTarget,
    browser_target,
    cached_game,
    is_allowed_browser_url,
)
from unifideck.stores.luna.luna_catalog import LunaCatalog, compose_detail_url
from unifideck.stores.luna.luna_subscription import LunaSubscription

# ── Generators constraining to the real Luna title / host input space ──

# A UUID-shaped product-id tail. Luna's single required id is of the form
# ``amzn1.adg.product.<UUID>`` (spike-verified F6); we vary the UUID freely
# but keep the ``amzn1.adg.product.`` prefix the catalog always emits.
_HEX = "0123456789abcdef"
_uuid = st.builds(
    lambda a, b, c, d, e: f"{a}-{b}-{c}-{d}-{e}",
    st.text(_HEX, min_size=8, max_size=8),
    st.text(_HEX, min_size=4, max_size=4),
    st.text(_HEX, min_size=4, max_size=4),
    st.text(_HEX, min_size=4, max_size=4),
    st.text(_HEX, min_size=12, max_size=12),
)
_product_ids = _uuid.map(lambda u: f"amzn1.adg.product.{u}")

# Title names: any non-trivial unicode text, including blanks that the writer
# falls back to the product id for. Kept clear of control chars that a JSON
# round-trip would alter.
_names = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")),
    min_size=0,
    max_size=60,
)

# Regional Luna hosts in the structural form the allow-list accepts
# (``luna.amazon.<tld>`` / ``luna.amazon.<tld>.<tld>``), given with and
# without a scheme / trailing slash so ``compose_detail_url`` normalisation is
# exercised too.
_tlds = st.sampled_from(["de", "co.uk", "fr", "it", "es", "ca", "com"])
_bare_hosts = _tlds.map(lambda tld: f"luna.amazon.{tld}")
_luna_hosts = st.one_of(
    _bare_hosts,
    _bare_hosts.map(lambda h: f"https://{h}"),
    _bare_hosts.map(lambda h: f"https://{h}/"),
)


class _Config:
    """Minimal stand-in for ``LunaConfig`` — the writer only reads ``luna_host``."""

    def __init__(self, luna_host: str) -> None:
        self.luna_host = luna_host


def _catalog(luna_host: str) -> LunaCatalog:
    """A ``LunaCatalog`` whose writer is all we exercise (no sync/source call)."""
    return LunaCatalog(
        config=_Config(luna_host),  # type: ignore[arg-type]
        subscription=LunaSubscription.__new__(LunaSubscription),
        source=None,  # type: ignore[arg-type]
    )


def _write_cache(path: Path, entry: dict[str, Any]) -> None:
    """Serialize one Luna entry into ``library_cache.json`` exactly as the sync
    layer does: ``asdict`` through a JSON round-trip (so the ``GameTag`` enum
    members become the plain ``"browser"`` / ``"luna"`` strings the launcher
    reads)."""
    payload = {"last_sync_time": None, "libraries": {"luna": [entry]}}
    path.write_text(json.dumps(payload), encoding="utf-8")


@settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    deadline=None,
)
@given(product_id=_product_ids, name=_names, luna_host=_luna_hosts)
def test_luna_library_entry_round_trips(
    product_id: str,
    name: str,
    luna_host: str,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Feature: amazon-luna-streaming, Property 5: Browser-game library entry round-trip.

    For any valid Luna title, writing it to ``library_cache.json`` as a
    browser-game entry (tag ``browser`` + ``metadata["browser_url"]`` set to the
    composed ``{luna_host}/game/{product_id}`` URL) and reading it back via
    ``cached_game`` / ``browser_target`` yields the same product id, the same
    composed URL, kind ``stream``, and a host that passes
    ``is_allowed_browser_url``.

    Validates: Requirements 4.3, 4.4, 4.5
    """
    from unifideck.stores.luna.luna_catalog import LunaTitle

    # Build the on-disk entry with the REAL catalog writer, then serialize it
    # the way the sync layer persists it (asdict -> JSON -> dict), so the
    # launcher reads exactly what production would write.
    game = _catalog(luna_host)._to_game(
        LunaTitle(luna_product_id=product_id, name=name, entitled=True),
    )
    entry = json.loads(json.dumps(asdict(game)))

    cache_path = tmp_path / "library_cache.json"
    _write_cache(cache_path, entry)

    # Point the stdlib-only launcher reader at the temp cache. The entry is a
    # per-example fresh file, so the function-scoped monkeypatch health check
    # is suppressed above intentionally.
    monkeypatch.setattr(browser_games, "LIBRARY_CACHE", str(cache_path))

    expected_url = compose_detail_url(luna_host, product_id)

    # The record is found by its product id (store_game_id), unchanged.
    record = cached_game("luna", product_id)
    assert record is not None
    assert record["store_game_id"] == product_id
    assert "browser" in record["tags"]
    assert "luna" in record["tags"]
    assert record["metadata"]["browser_url"] == expected_url

    # And it resolves back to a lossless stream BrowserTarget.
    target = browser_target("luna", product_id)
    assert target == BrowserTarget(url=expected_url, kind="stream")
    assert target is not None
    assert target.kind == "stream"
    assert target.url == expected_url
    assert is_allowed_browser_url(target.url, "stream")
