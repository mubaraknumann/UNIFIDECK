"""LunaCatalog catalog models + source-strategy interface.

This module currently defines the **data models** and the **strategy
boundary** for Luna catalog/entitlement acquisition (task 8.2, Req 3.1,
4.1). It deliberately stops at the interface: the Strategy B DOM scrape
(task 8.3) and the ``LunaSubscription``/reconciliation glue (task 8.5) are
implemented separately.

Catalog/entitlement acquisition is the lowest-confidence, highest-risk
part of the Luna feature (design Open Question Q4): the feasibility test
exercised **no** Luna catalog or entitlement API, so the design does not
commit to one. Instead the data source is a pluggable
:class:`CatalogSource` strategy with two intended implementations:

* **Strategy A — authenticated API** (preferred, *unverified*). Would call
  the ``catalog_endpoint`` / ``subscription_endpoint`` on
  :class:`~unifideck.stores.luna.luna_config.LunaConfig` with the Luna
  session cookies. Not implemented here and not selected by default,
  because no such endpoint has been confirmed to exist.
* **Strategy B — CDP-driven DOM scrape** (fallback, always available).
  Drives the Luna library pages in the spoofed streaming browser and reads
  the rendered title cards. This is the default because it uses only what
  the feasibility test proved works (the web UI).

Every result records *which* path produced it via the ``source`` field
(:class:`EntitlementSet.source`, :data:`LunaTitle` carries no source — the
set it came from does), so diagnostics can tell an API answer from a
scraped one (design "Selection and risk handling").

Mirrors the *shape* of ``microsoft_catalog.py`` (strategy object, frozen
model dataclasses) without copying its account-scoped-API mechanics, which
do not apply until a Luna API is confirmed.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from unifideck.core.types import Game, GameTag

# The launcher's browser-games module owns the on-disk routing keys/tags a
# Luna library entry must carry so ``browser_games.browser_target`` resolves
# it back to a ``stream`` ``BrowserTarget``. Imported here (as
# ``microsoft_catalog`` does) rather than re-declared, so the writer and the
# launcher agree on one definition.
from unifideck.launcher.browser_games import BROWSER_URL_KEY
from unifideck.stores.luna.luna_cdp import attach_to_page_target

if TYPE_CHECKING:
    from unifideck.auth.edge_browser.cdp_client import EdgeCDPClient
    from unifideck.stores.luna.luna_config import LunaConfig
    from unifideck.stores.luna.luna_subscription import LunaSubscription

#: The store tag every Luna library entry additionally carries (Req 4.5),
#: alongside ``GameTag.BROWSER``. Drives the frontend's Luna Play control and
#: the per-store reconcile.
LUNA_STORE_TAG = "luna"

logger = logging.getLogger(__name__)

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


# ─────────────────────────────────────────────────────────────────────
# Strategy B — CDP-driven DOM scrape (task 8.3, Req 3.7, 4.1, 4.4)
# ─────────────────────────────────────────────────────────────────────
#
# This is the default, always-available catalog source: it drives the
# rendered Luna library pages in the already-spoofed streaming browser over
# the SAME ``BrowserCdpSession`` abstraction / page-target-attach /
# ``Runtime.evaluate`` pattern established in
# :mod:`unifideck.stores.luna.luna_platform_spoofer` and reused in
# :mod:`unifideck.stores.luna.luna_play_activator`. No undiscovered API is
# needed — only the web UI the feasibility test proved works.
#
# The two concerns are kept strictly separate:
#
#   * The impure CDP driving (:class:`DomScrapeCatalogSource`) resolves the
#     browser websocket on ``browser_game_cdp_port()``, attaches to the page
#     target, and runs ONE ``Runtime.evaluate`` (``returnByValue``) that
#     returns a plain list of card dicts.
#   * The DOM-shape assumptions — WHICH selectors to query and HOW to pull
#     the product id / name / Prime marker out of a tile element — live
#     ONLY inside the injected JS (:data:`_LIBRARY_SCRAPE_EXPRESSION`),
#     which returns an already-normalised list of
#     ``{"product_id", "name", "prime"}`` dicts.
#   * The pure parser (:func:`parse_library_cards`) takes that list of card
#     dicts and returns ``(list[LunaTitle], EntitlementSet)``. It has no I/O
#     and no selector knowledge, so task 8.4 can unit-test it against HTML
#     fixtures (once task 8.1 captures real HTML) without a browser.
#
# ╔═══════════════════════════════════════════════════════════════════╗
# ║ SPIKE-VERIFIED (F6, catalog/launch spike) — the browse-view tile    ║
# ║ structure below was confirmed live on a Steam Deck against          ║
# ║ luna.amazon.de/browse?quick_search=my_games (180 real tiles):       ║
# ║   • each title tile is div[data-test-id="game_tile"][role="button"] ║
# ║     with id="game_tile_amzn1.adg.product.<UUID>"                     ║
# ║   • the stable per-title id is the amzn1.adg.product.<UUID> product  ║
# ║     id (strip the "game_tile_" prefix) — there is NO slug and NO     ║
# ║     ASIN in the library DOM                                          ║
# ║   • the name is read from the tile aria-label / nested img alt       ║
# ║   • the launch URL is {luna_host}/game/{product_id}; Luna redirects  ║
# ║     it to the canonical slug+ASIN detail page                        ║
# ║ Still BEST-EFFORT and only live-probed (not saved as a fixture):     ║
# ║ the exact browse-tile NAME source and the Prime-inclusion marker —   ║
# ║ "In Prime enthalten" was seen on the DETAIL page, not reliably on    ║
# ║ browse tiles, so tile-level entitlement is unconfirmed. When task    ║
# ║ 8.4 saves real browse HTML, ONLY the JS extraction below should      ║
# ║ need touching — the pure parser and the CDP driver stay unchanged.   ║
# ╚═══════════════════════════════════════════════════════════════════╝

#: The German Prime-inclusion marker. Best-effort: the spike saw it on the
#: detail page, not reliably on browse tiles, so a browse tile carrying it
#: is treated as entitled but its absence is NOT taken as "not entitled"
#: (Req 3.7 caveat).
PRIME_INCLUDED_MARKER = "In Prime enthalten"

#: How long to wait for the browse SPA's tiles to render before scraping, and
#: how often to re-check. The browse view populates its ``game_tile`` nodes a
#: few seconds after the page target exists; scraping too early read an empty
#: DOM and falsely reported zero titles / subscription_required.
_TILE_WAIT_TIMEOUT_S = 15.0
_TILE_POLL_INTERVAL_S = 0.5

#: The browse view lazy-loads / virtualises tiles on scroll, so only the
#: first screenful is in the DOM at first. Scroll to the bottom repeatedly
#: until the tile count stops growing, bounded by this budget; wait this long
#: after each scroll for the next batch to render.
_TILE_SCROLL_TIMEOUT_S = 30.0
_TILE_SCROLL_SETTLE_S = 1.2

#: Scroll every scrollable context to the bottom to trigger the next lazy
#: batch: the window, the document element, and any element taller than its
#: viewport (the browse grid often scrolls inside its own container, not the
#: window). Returns nothing; the caller re-counts tiles afterwards.
_SCROLL_TO_BOTTOM_EXPRESSION = r"""
(() => {
  try { window.scrollTo(0, document.body.scrollHeight); } catch (e) {}
  try {
    const de = document.scrollingElement || document.documentElement;
    if (de) de.scrollTop = de.scrollHeight;
  } catch (e) {}
  for (const el of document.querySelectorAll('*')) {
    if (el.scrollHeight > el.clientHeight + 50) {
      try { el.scrollTop = el.scrollHeight; } catch (e) {}
    }
  }
})()
""".strip()

#: Count the rendered browse-view game tiles (same selector family as the
#: scrape). ``> 0`` means the library has rendered and the scrape can run.
_TILE_COUNT_EXPRESSION = (
    'document.querySelectorAll('
    "'[data-test-id=\"game_tile\"][role=\"button\"]'"
    ").length"
)

#: The JS evaluated over CDP (``returnByValue``) to read the rendered
#: browse-view title cards. It isolates EVERY DOM / selector assumption:
#: it selects the clickable ``game_tile`` nodes, recovers the product id by
#: stripping the ``game_tile_`` prefix off the element ``id``, reads the
#: name from aria-label / nested img alt / text, and flags any Prime marker
#: text present on the tile. It returns a plain list of
#: ``{"product_id", "name", "prime"}`` dicts, so no selector knowledge leaks
#: into the pure parser. Returns ``[]`` for an empty library (a legitimate
#: zero-titles result, Req 4.9) — distinct from an evaluate that throws
#: (a transport/parse failure the driver turns into a raise, Req 4.2/4.6).
_LIBRARY_SCRAPE_EXPRESSION = r"""
(() => {
  const PRIME_MARKER = "In Prime enthalten";
  // A tile's product id is one of TWO Amazon id namespaces, both seen live:
  //   amzn1.adg.product.<uuid>   (most catalog titles)
  //   amzn1.luna.product.<uuid>  (Luna-native / newer titles, e.g. STAR WARS
  //                               Galactic Racer) — these were silently
  //                               dropped when only adg was matched.
  // Match either, capturing the full id up to any trailing _suffix
  // (e.g. _impression / _imageUrl / _test_id on sibling wrappers).
  const ID_RE = /amzn1\.(?:adg|luna)\.product\.[0-9a-fA-F-]+/;
  // Clickable browse-view game tiles: the role=button node carries
  // data-test-id="game_tile"; the sibling *_impression wrapper has no
  // role=button and is excluded.
  const tiles = Array.from(document.querySelectorAll(
    '[data-test-id="game_tile"][role="button"]'
  ));
  const seen = new Set();
  const out = [];
  for (const tile of tiles) {
    const rawId = tile.id || "";
    const m = rawId.match(ID_RE);
    if (!m) continue;                    // not a product tile
    const productId = m[0];
    if (!productId || seen.has(productId)) continue;
    seen.add(productId);
    const img = tile.querySelector("img");
    const name = (
      tile.getAttribute("aria-label") ||
      (img ? img.getAttribute("alt") : "") ||
      (tile.textContent || "").trim() ||
      productId
    ).trim();
    const text = (tile.textContent || "").trim();
    out.push({
      product_id: productId,
      name: name,
      prime: text.indexOf(PRIME_MARKER) !== -1,
    });
  }
  return out;
})()
""".strip()


class LunaCatalogScrapeError(Exception):
    """A transport/parse failure of the DOM scrape (Req 4.2, 4.6).

    Raised (not returned) by :class:`DomScrapeCatalogSource` when the scrape
    could not run or its result could not be read — no page target, no
    browser websocket, the ``Runtime.evaluate`` threw, or the returned value
    was not the expected list of card dicts. The store maps this to a
    synchronization error and leaves existing library entries unchanged
    (Req 4.2, 4.6), kept distinct from a legitimately **empty** library,
    which returns ``[]`` / an empty :class:`EntitlementSet` (Req 4.9).
    """


def parse_library_cards(
    cards: object,
) -> tuple[list[LunaTitle], EntitlementSet]:
    """Parse scraped browse-view title cards into titles + an entitlement set.

    PURE and deterministic — no I/O, no selector knowledge. ``cards`` is the
    normalised structure the injected DOM query returns: a list of dicts
    shaped ``{"product_id": str, "name": str, "prime": bool}``. All
    CSS/DOM-shape assumptions live in :data:`_LIBRARY_SCRAPE_EXPRESSION`, so
    when real browse HTML is saved (task 8.4) only that JS needs adjusting
    and this parser stays unchanged.

    Returns ``(titles, entitlements)`` where:

    * A card missing its ``product_id`` is **malformed** and skipped (it is
      the single required id — Req 4.7); the catalog writer (task 8.5)
      preserves any pre-existing entry for it.
    * ``entitled`` is ``True`` when the card carries the Prime marker
      (``prime``). This is best-effort: the spike saw the marker on the
      detail page, not reliably on browse tiles (Req 3.7 caveat), so its
      absence is not treated as proof of non-entitlement here.
    * The :class:`EntitlementSet` collects the product ids of the entitled
      titles and is always stamped ``source="dom-scrape"`` (Req 4.1). An
      **empty** ``cards`` list legitimately yields zero titles and an empty
      entitlement set (Req 4.9) — not an error.

    This parser never raises on malformed card content: a bad card is simply
    skipped. A transport/parse FAILURE (the scrape could not run or returned
    a non-list) is the driver's responsibility and surfaces as
    :class:`LunaCatalogScrapeError`, so "zero titles" and "scrape failed"
    stay distinct (Req 4.2/4.6 vs 4.9).
    """
    if not isinstance(cards, list):
        # Defensive: the driver validates the evaluate result before calling
        # us, but keep the pure parser total — a non-list is "no titles".
        return [], EntitlementSet(entitlements=(), source="dom-scrape")

    titles: list[LunaTitle] = []
    entitled_ids: list[str] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        product_id = _clean_str(card.get("product_id"))
        if not product_id:
            # Malformed card (missing product id): skip, cannot build a
            # detail URL (Req 4.7). The writer preserves the existing entry.
            continue
        # Every tile on the ``my_games`` browse view IS the account's own
        # playable library, so a listed tile is entitled by definition. The
        # Prime marker ("In Prime enthalten") only shows HOW a title is
        # included and is unreliable on browse tiles (the spike saw it on the
        # detail page), so keying entitlement off it read every title as
        # not-entitled → a false ``subscription_required`` with zero games.
        # The browse-view presence is the entitlement signal; ``prime`` is
        # retained on the card only as supplementary info.
        entitled = True
        name = _clean_str(card.get("name")) or product_id
        titles.append(
            LunaTitle(
                luna_product_id=product_id,
                name=name,
                entitled=entitled,
            ),
        )
        if entitled:
            entitled_ids.append(product_id)

    return titles, EntitlementSet(
        entitlements=tuple(entitled_ids), source="dom-scrape",
    )


def compose_detail_url(luna_host: str, product_id: str) -> str:
    """Compose the Luna_Title_Detail_URL from the host and product id.

    ``{luna_host}/game/{product_id}`` (spike-verified F6): navigating here
    makes Luna redirect to the canonical slug+ASIN detail page with the Play
    button, so slug/ASIN are never needed at sync time. This is the value
    the catalog writer (task 8.5) stores as ``metadata["browser_url"]``.
    ``luna_host`` may be given with or without a scheme / trailing slash.
    """
    host = luna_host.strip().rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = "https://" + host
    return f"{host}/game/{product_id}"


def _clean_str(value: object) -> str:
    """Return ``value`` as a stripped string, or ``""`` for non-strings."""
    return value.strip() if isinstance(value, str) else ""


def extract_cards_from_evaluate(result: dict[str, object]) -> list[object]:
    """Extract the card list from the library-scrape ``Runtime.evaluate`` result.

    ``result`` is the payload from evaluating
    :data:`_LIBRARY_SCRAPE_EXPRESSION` with ``returnByValue``; its shape is
    ``{"result": {"type": "object", "value": [ {card}, ... ]}}``. A thrown
    evaluation carries ``exceptionDetails`` instead.

    PURE and deterministic. Unlike the play-activator probes (which swallow a
    transient error as a retry), an error or a missing value here is a real
    scrape FAILURE: it raises :class:`LunaCatalogScrapeError` so the store
    leaves existing entries unchanged and reports a sync error (Req 4.2/4.6),
    rather than silently reporting "zero titles" (Req 4.9). An evaluate that
    genuinely returns ``[]`` is passed through untouched as a legitimate
    empty library.
    """
    exception = result.get("exceptionDetails")
    if exception:
        raise LunaCatalogScrapeError(
            f"Luna library scrape failed: DOM query raised {exception!r}",
        )
    inner = result.get("result")
    if not isinstance(inner, dict) or "value" not in inner:
        raise LunaCatalogScrapeError(
            "Luna library scrape failed: DOM query returned no value "
            f"(result {result!r})",
        )
    value = inner.get("value")
    if not isinstance(value, list):
        raise LunaCatalogScrapeError(
            "Luna library scrape failed: DOM query returned a non-list "
            f"value {value!r}",
        )
    return value


class DomScrapeCatalogSource:
    """Strategy B: read the Luna catalog from the rendered library pages.

    A concrete :class:`CatalogSource` (satisfies the Protocol from task 8.2;
    stamps ``source="dom-scrape"`` on every :class:`EntitlementSet`). It is
    the default source because it uses only the web UI the feasibility test
    proved works — no undiscovered Luna API (design Q4).

    Constructed WITHOUT a :class:`LunaStore` (nothing but an optional
    injected session factory) so it runs in the launcher subprocess, exactly
    like :class:`~unifideck.stores.luna.luna_platform_spoofer.LunaPlatformSpoofer`
    and :class:`~unifideck.stores.luna.luna_play_activator.LunaPlayActivator`.

    The CDP driving reuses the SAME I/O seam those two use: it resolves the
    browser websocket on ``browser_game_cdp_port()`` via ``EdgeCDPClient``,
    opens a :class:`BrowserCdpSession`, attaches to the loaded library page
    target in CDP flatten mode, and runs ONE ``Runtime.evaluate``
    (``returnByValue``). The returned normalised card list is handed to the
    pure :func:`parse_library_cards`; the selectors live only in
    :data:`_LIBRARY_SCRAPE_EXPRESSION`.

    Error contract (matches the :class:`CatalogSource` Protocol):

    * A transport/parse FAILURE — no page target, no browser websocket, the
      evaluate threw, or a non-list result — raises
      :class:`LunaCatalogScrapeError`, so the store leaves existing entries
      and reports a sync error (Req 4.2, 4.6).
    * An EMPTY library legitimately returns ``[]`` / an empty
      :class:`EntitlementSet` (Req 4.9), never a raise.
    """

    def __init__(
        self, *, session_factory: object | None = None,
    ) -> None:
        """Build the scrape source.

        ``session_factory`` is an optional ``(ws_url) -> BrowserCdpSession``
        hook, used by tests to inject a mock session returning a scripted
        evaluate result. It defaults to the aiohttp-backed browser session
        the spoofer/activator use, so production needs no argument.
        """
        if session_factory is None:
            from unifideck.stores.luna.luna_platform_spoofer import (
                _AiohttpBrowserCdpSession,
            )

            self._session_factory = _AiohttpBrowserCdpSession
        else:
            self._session_factory = session_factory

    async def fetch_titles(
        self, session: EdgeCDPClient, cfg: LunaConfig,
    ) -> list[LunaTitle]:
        """Scrape the account's Luna title list from the library page.

        Drives the DOM scrape and returns only the title list; the
        entitlement derivation shares the same scrape via
        :meth:`fetch_entitlements`. An empty library returns ``[]`` (Req 4.9);
        a transport/parse failure raises :class:`LunaCatalogScrapeError`
        (Req 4.2, 4.6).
        """
        titles, _ = await self._scrape(session)
        return titles

    async def fetch_entitlements(
        self, session: EdgeCDPClient, cfg: LunaConfig,
    ) -> EntitlementSet:
        """Scrape the account's entitlements from the library page (Req 3.7).

        Prime-included titles ("In Prime enthalten", F1) are treated as
        entitled; the returned :class:`EntitlementSet` is stamped
        ``source="dom-scrape"``. An empty library yields an empty set
        (``subscription_required`` upstream, Req 3.3/3.4); a failure raises
        :class:`LunaCatalogScrapeError` (Req 4.2, 4.6).
        """
        _, entitlements = await self._scrape(session)
        return entitlements

    async def _scrape(
        self, cdp: EdgeCDPClient,
    ) -> tuple[list[LunaTitle], EntitlementSet]:
        """Run the one DOM scrape and parse it, over a live CDP client.

        Resolves the browser websocket on ``browser_game_cdp_port()``, opens
        a :class:`BrowserCdpSession`, attaches to the loaded library page
        target, and delegates to :meth:`run_scrape`. Raises
        :class:`LunaCatalogScrapeError` when the browser websocket is
        unavailable (a transport failure — Req 4.2/4.6).
        """
        ws_url = cdp.get_browser_ws_url()
        if not ws_url:
            raise LunaCatalogScrapeError(
                "cannot scrape Luna library: no browser websocket URL on the "
                "browser-game CDP port",
            )
        session = self._session_factory(ws_url)
        try:
            session_id = await self._attach_to_page_target(session)
            return await self.run_scrape(session, session_id)
        finally:
            close = getattr(session, "close", None)
            if close is not None:
                await close()

    async def run_scrape(
        self, session: object, session_id: str,
    ) -> tuple[list[LunaTitle], EntitlementSet]:
        """Scrape + parse over an arbitrary ``session`` (testable core).

        Separated from :meth:`_scrape` so it can be driven against a mock
        :class:`BrowserCdpSession` returning a scripted evaluate result. Runs
        the single ``Runtime.evaluate`` (:data:`_LIBRARY_SCRAPE_EXPRESSION`,
        ``returnByValue``), extracts the normalised card list via the pure
        :func:`extract_cards_from_evaluate` (which raises on a failed/
        unreadable evaluate — Req 4.2/4.6), and hands it to the pure
        :func:`parse_library_cards`. An evaluate that returns ``[]`` flows
        straight through as a legitimate empty library (Req 4.9).
        """
        # The browse view is a SPA: its tiles render a few seconds after the
        # page target exists. Scraping immediately read an empty DOM and
        # reported zero titles (a false subscription_required). Wait for the
        # tiles to appear (bounded) before the real scrape.
        await self._await_tiles(session, session_id)
        result = await session.send(
            "Runtime.evaluate",
            {
                "expression": _LIBRARY_SCRAPE_EXPRESSION,
                "returnByValue": True,
            },
            session_id=session_id,
        )
        cards = extract_cards_from_evaluate(result)
        titles, entitlements = parse_library_cards(cards)
        logger.info(
            "[Luna] DOM scrape parsed %d title(s), %d entitled",
            len(titles), len(entitlements.entitlements),
        )
        return titles, entitlements

    async def _await_tiles(self, session: object, session_id: str) -> None:
        """Poll until browse-view game tiles have rendered (bounded).

        The Luna browse SPA populates its ``game_tile`` nodes a few seconds
        after the page target exists; scraping before then reads an empty DOM
        and reports zero titles. Polls the tile count up to
        :data:`_TILE_WAIT_TIMEOUT_S`, returning as soon as any tile is present
        (or when the budget elapses — then the scrape runs anyway and an
        empty result is handled by the caller as before).
        """
        import asyncio
        import time

        # Phase 1: wait for the first tiles to render.
        deadline = time.monotonic() + _TILE_WAIT_TIMEOUT_S
        while True:
            if await self._tile_count(session, session_id) > 0:
                break
            if time.monotonic() >= deadline:
                logger.warning(
                    "[Luna] no browse tiles after %.0fs — scraping anyway",
                    _TILE_WAIT_TIMEOUT_S,
                )
                return
            await asyncio.sleep(_TILE_POLL_INTERVAL_S)

        # Phase 2: the browse view lazy-loads tiles on scroll, so only the
        # first screenful (~80) is in the DOM initially and the rest of the
        # library is missing from a naive scrape. Scroll to the bottom and
        # re-count until the count stops growing (fully loaded) or the scroll
        # budget elapses. Best-effort: on timeout the scrape runs on what has
        # loaded.
        scroll_deadline = time.monotonic() + _TILE_SCROLL_TIMEOUT_S
        last_count = await self._tile_count(session, session_id)
        stable_rounds = 0
        while time.monotonic() < scroll_deadline:
            await session.send(
                "Runtime.evaluate",
                {"expression": _SCROLL_TO_BOTTOM_EXPRESSION},
                session_id=session_id,
            )
            await asyncio.sleep(_TILE_SCROLL_SETTLE_S)
            count = await self._tile_count(session, session_id)
            if count <= last_count:
                stable_rounds += 1
                if stable_rounds >= 2:  # two scrolls added nothing → done
                    break
            else:
                stable_rounds = 0
            last_count = max(last_count, count)
        logger.info("[Luna] browse scroll settled at ~%d tiles", last_count)

    async def _tile_count(self, session: object, session_id: str) -> int:
        """Current rendered browse-view game-tile count (0 on any failure)."""
        result = await session.send(
            "Runtime.evaluate",
            {"expression": _TILE_COUNT_EXPRESSION, "returnByValue": True},
            session_id=session_id,
        )
        count = (((result or {}).get("result") or {}).get("value")) or 0
        return count if isinstance(count, int) else 0

    async def _attach_to_page_target(self, session: object) -> str:
        """Return a CDP session id attached to the active Luna page target.

        Mirrors
        :meth:`LunaPlatformSpoofer._attach_to_page_target` /
        :meth:`LunaPlayActivator._attach_to_page_target`: asks the browser
        for its targets and attaches (flatten mode) to the first ``page``
        target — the loaded Luna library page the scrape must run against.
        Raises :class:`LunaCatalogScrapeError` when no page target exists (a
        transport failure, Req 4.2/4.6).

        Delegates the shared CDP page-target-attach step to
        :func:`unifideck.stores.luna.luna_cdp.attach_to_page_target`.
        """
        return await attach_to_page_target(
            session,
            raise_no_target=lambda: LunaCatalogScrapeError(
                "cannot scrape Luna library: no page target found on the "
                "browser-game CDP port",
            ),
            raise_no_session=lambda: LunaCatalogScrapeError(
                "cannot scrape Luna library: failed to attach to the Luna "
                "page target",
            ),
        )


# ─────────────────────────────────────────────────────────────────────
# Catalog writer + library-cache reconciliation (task 8.5, Req 3/4)
# ─────────────────────────────────────────────────────────────────────
#
# ``LunaCatalog`` is the orchestration seam the store's ``get_library`` calls.
# It runs the subscription gate (:class:`LunaSubscription`), retrieves the
# title list over the SAME selected :class:`CatalogSource` strategy, builds one
# browser-game :class:`Game` per VALID title, and reconciles that against the
# Luna entries already in ``library_cache.json`` so the store returns the
# authoritative value its sync layer expects.
#
# The return contract matches ``SyncService`` reconciliation (and mirrors
# ``MicrosoftStore.get_library``):
#
#   * a ``list[Game]`` is AUTHORITATIVE — the sync layer replaces every Luna
#     shortcut with it, so a previously-synced title that is no longer in the
#     retrieved catalog is dropped simply by being absent (Req 4.8);
#   * an EMPTY list ``[]`` means the catalog answered with zero titles, which
#     removes all Luna entries (Req 4.9);
#   * ``None`` means "could not read — keep what is there": every failure path
#     (timeout, unreachable, scrape error, entitlement-source-unavailable)
#     returns ``None`` so the existing Luna library entries are left unchanged
#     (Req 4.2, 4.6, 3.6).
#
# Malformed titles (missing ``luna_product_id``) are already dropped by
# :func:`parse_library_cards`, so they never reach the writer as titles; to
# honour Req 4.7 ("preserve the corresponding existing entry") the writer reads
# the previous Luna entries from ``library_cache.json`` and, when any title was
# malformed this run, carries the pre-existing entries forward rather than
# letting the authoritative replace drop them.


class CatalogSyncStatus(Enum):
    """The externally observable result of one catalog synchronization.

    Distinct from :class:`~unifideck.stores.luna.luna_subscription.SubscriptionOutcome`:
    the subscription outcome gates WHETHER a sync runs; this reports HOW the
    sync itself ended, matching the design "Error Handling" table (Req 3/4).
    """

    #: One or more valid titles written (Req 4.3).
    SYNCED = "synced"
    #: Catalog answered with zero titles — all Luna entries removed (Req 4.9).
    SYNCED_ZERO = "synced_zero"
    #: Some titles were malformed (missing id); the valid ones were written and
    #: the malformed titles' pre-existing entries preserved (Req 4.7).
    SYNCED_WITH_MALFORMED = "synced_with_malformed"
    #: Account has zero entitlements — subscription required (Req 3.4). No
    #: entries changed.
    SUBSCRIPTION_REQUIRED = "subscription_required"
    #: Entitlement retrieval failed, distinct from subscription-required
    #: (Req 3.5). Entries left unchanged.
    ENTITLEMENT_ERROR = "entitlement_error"
    #: No entitlement interface at all (Req 3.6). Entries left unchanged.
    ENTITLEMENT_SOURCE_UNAVAILABLE = "entitlement_source_unavailable"
    #: Catalog retrieval timed out / was unreachable / errored (Req 4.2, 4.6).
    #: Entries left unchanged.
    SYNC_ERROR = "sync_error"


@dataclass(frozen=True)
class CatalogSyncResult:
    """The outcome of :meth:`LunaCatalog.sync`.

    ``games`` is the value the store's ``get_library`` returns to the sync
    layer: a ``list[Game]`` (authoritative, including the empty list for zero
    titles), or ``None`` to leave the existing Luna entries unchanged. ``status``
    is the distinct Req 3/4 state the store surfaces to the frontend, and
    ``error`` carries a short diagnostic for the failure statuses.
    """

    status: CatalogSyncStatus
    games: list[Game] | None
    error: str | None = None

    @property
    def changed_library(self) -> bool:
        """Whether this result replaces the Luna library (vs leaving it)."""
        return self.games is not None


class LunaCatalog:
    """Retrieves the Luna catalog and reconciles it into ``library_cache.json``.

    Orchestrates the subscription gate + title retrieval over one selected
    :class:`CatalogSource` (default Strategy B, the DOM scrape) and maps the
    whole thing onto the distinct Req 3/4 outcomes. Strategy-agnostic by
    construction: it drives only the :class:`CatalogSource` boundary and the
    :class:`~unifideck.stores.luna.luna_subscription.LunaSubscription` gate, so
    the outcome mapping is identical for an API or a scrape (task 8.7).

    Mirrors the role of ``MicrosoftCatalogReader`` + ``MicrosoftStore.get_library``'s
    reconciliation: produce the authoritative browser-game ``Game`` list, or
    ``None`` to keep the existing entries.
    """

    def __init__(
        self,
        config: LunaConfig,
        subscription: LunaSubscription,
        source: CatalogSource,
        *,
        timeout_s: float = 30.0,
    ) -> None:
        """Build the catalog orchestrator.

        ``subscription`` is the gate (reused so its preserved entitlement set
        survives across syncs, Req 3.2); ``source`` is the SAME selected
        :class:`CatalogSource` the subscription uses, so a single strategy
        choice drives both. ``timeout_s`` is the catalog-retrieval budget
        (Req 4.1), injectable for tests.
        """
        self._config = config
        self._subscription = subscription
        self._source = source
        self._timeout_s = timeout_s

    async def sync(
        self,
        session: EdgeCDPClient,
        previous_entries: list[dict[str, object]] | None = None,
    ) -> CatalogSyncResult:
        """Run one full catalog synchronization and reconcile it.

        ``previous_entries`` are the Luna library entries already in
        ``library_cache.json`` (each a serialized :class:`Game` dict), used to
        preserve a malformed title's pre-existing entry (Req 4.7). The store
        reads them from the cache and passes them in so this stays I/O-free and
        unit-testable against a temp cache (design Property 6).

        Flow (design "Catalog acquisition strategy"):

        1. Subscription gate (:class:`LunaSubscription`): zero entitlements →
           :attr:`CatalogSyncStatus.SUBSCRIPTION_REQUIRED` (Req 3.4); retrieval
           failure → :attr:`ENTITLEMENT_ERROR` (Req 3.5); no interface →
           :attr:`ENTITLEMENT_SOURCE_UNAVAILABLE` (Req 3.6). All three leave the
           library unchanged (``games=None``).
        2. Title retrieval under the 30s budget (Req 4.1); timeout / scrape
           failure → :attr:`SYNC_ERROR` (Req 4.2, 4.6), library unchanged.
        3. Reconcile the valid titles into the authoritative ``Game`` list.
        """
        from unifideck.stores.luna.luna_subscription import SubscriptionOutcome

        verdict = await self._subscription.determine(session)
        if verdict.outcome is SubscriptionOutcome.SUBSCRIPTION_REQUIRED:
            return CatalogSyncResult(
                status=CatalogSyncStatus.SUBSCRIPTION_REQUIRED, games=None,
            )
        if verdict.outcome is SubscriptionOutcome.ENTITLEMENT_ERROR:
            return CatalogSyncResult(
                status=CatalogSyncStatus.ENTITLEMENT_ERROR,
                games=None, error=verdict.error,
            )
        if verdict.outcome is SubscriptionOutcome.ENTITLEMENT_SOURCE_UNAVAILABLE:
            return CatalogSyncResult(
                status=CatalogSyncStatus.ENTITLEMENT_SOURCE_UNAVAILABLE,
                games=None, error=verdict.error,
            )

        try:
            titles = await asyncio.wait_for(
                self._source.fetch_titles(session, self._config),
                timeout=self._timeout_s,
            )
        except TimeoutError:
            logger.warning(
                "[LunaCatalog] catalog retrieval exceeded %.0fs — leaving "
                "existing Luna library entries unchanged", self._timeout_s,
            )
            return CatalogSyncResult(
                status=CatalogSyncStatus.SYNC_ERROR, games=None,
                error="timeout",
            )
        except Exception as exc:
            logger.warning(
                "[LunaCatalog] catalog retrieval failed (%s: %s) — leaving "
                "existing Luna library entries unchanged",
                type(exc).__name__, exc,
            )
            return CatalogSyncResult(
                status=CatalogSyncStatus.SYNC_ERROR, games=None,
                error=f"{type(exc).__name__}: {exc}",
            )

        return self._reconcile(titles, previous_entries or [])

    def _reconcile(
        self, titles: list[LunaTitle], previous_entries: list[dict[str, object]],
    ) -> CatalogSyncResult:
        """Build the authoritative ``Game`` list from the retrieved titles.

        Pure (no I/O). A title is VALID iff it has a ``luna_product_id``; the
        scraper already drops id-less cards, but the writer re-checks so the
        contract holds for any :class:`CatalogSource` (an API strategy could
        return one). Valid titles are additionally **deduplicated on
        ``luna_product_id``** — first occurrence wins, order preserved — so the
        authoritative list has exactly one entry per product id regardless of
        the :class:`CatalogSource`. The DOM scraper already dedupes via its
        ``seen`` set (Strategy B unaffected); this hardens the contract for a
        future Strategy A whose API could repeat an id, which would otherwise
        emit two Games with the same ``store_game_id`` (a duplicate library
        entry). Malformed titles are skipped (Req 4.7); when any were
        malformed, the pre-existing entries whose ids are not otherwise
        produced this run are carried forward so they are preserved unchanged
        (Req 4.7), rather than being dropped by the authoritative replace.

        * zero retrieved titles → ``[]`` → remove all Luna entries, status
          :attr:`SYNCED_ZERO` (Req 4.9);
        * all valid → ``list[Game]`` → the sync layer drops titles no longer
          present (Req 4.8), status :attr:`SYNCED`;
        * some malformed → valid ones written + malformed titles' pre-existing
          entries preserved, status :attr:`SYNCED_WITH_MALFORMED` (Req 4.7).
        """
        valid: list[LunaTitle] = []
        seen_ids: set[str] = set()
        malformed = 0
        for title in titles:
            if not title.luna_product_id:
                malformed += 1
                continue
            if title.luna_product_id in seen_ids:
                # Duplicate product id: first occurrence wins so the
                # authoritative list carries exactly one entry per id,
                # regardless of the CatalogSource.
                continue
            seen_ids.add(title.luna_product_id)
            valid.append(title)

        if not titles:
            # Catalog answered zero titles: authoritative empty list removes
            # every Luna entry (Req 4.9).
            logger.info(
                "[LunaCatalog] catalog returned zero titles — removing all "
                "Luna library entries",
            )
            return CatalogSyncResult(
                status=CatalogSyncStatus.SYNCED_ZERO, games=[],
            )

        games = [self._to_game(t) for t in valid]
        produced_ids = {t.luna_product_id for t in valid}

        if malformed:
            carried = self._carry_forward_unproduced(
                previous_entries, produced_ids,
            )
            logger.warning(
                "[LunaCatalog] %d malformed title(s) skipped; preserving %d "
                "pre-existing entr(y/ies), wrote %d valid title(s)",
                malformed, len(carried), len(games),
            )
            return CatalogSyncResult(
                status=CatalogSyncStatus.SYNCED_WITH_MALFORMED,
                games=games + carried,
                error=f"{malformed} malformed title(s)",
            )

        logger.info(
            "[LunaCatalog] synced %d Luna title(s)", len(games),
        )
        return CatalogSyncResult(
            status=CatalogSyncStatus.SYNCED, games=games,
        )

    def _to_game(self, title: LunaTitle) -> Game:
        """Build one browser-game :class:`Game` for a valid Luna title.

        Carries ``GameTag.BROWSER`` + the ``luna`` store tag (Req 4.3, 4.5) and
        ``metadata["browser_url"]`` composed as ``{luna_host}/game/{product_id}``
        (Req 4.4) so ``browser_games.browser_target`` resolves it back to a
        ``stream`` :class:`~unifideck.launcher.browser_games.BrowserTarget`.
        ``app_id=0`` is assigned deterministically by the sync layer; the store
        installs nothing, so ``installed`` is ``False``.
        """
        detail_url = compose_detail_url(
            self._config.luna_host, title.luna_product_id,
        )
        return Game(
            app_id=0,
            store="luna",
            store_game_id=title.luna_product_id,
            title=title.name or title.luna_product_id,
            installed=False,
            tags=[GameTag.BROWSER, LUNA_STORE_TAG],
            metadata={BROWSER_URL_KEY: detail_url},
        )

    @staticmethod
    def _carry_forward_unproduced(
        previous_entries: list[dict[str, object]], produced_ids: set[str],
    ) -> list[Game]:
        """Pre-existing Luna entries whose id was not produced this run.

        For Req 4.7: a malformed title must not cause a pre-existing entry to be
        dropped by the authoritative replace. Since a malformed title has no id
        to match, we conservatively preserve every previously-synced Luna entry
        whose id is not among this run's produced ids — those are the entries a
        skip could otherwise have removed. Entries that are missing their id or
        ``browser_url`` are not reconstructable and are left out.
        """
        carried: list[Game] = []
        for entry in previous_entries:
            if not isinstance(entry, dict):
                continue
            game_id = entry.get("store_game_id")
            if not isinstance(game_id, str) or not game_id:
                continue
            if game_id in produced_ids:
                continue
            metadata = entry.get("metadata")
            url = ""
            if isinstance(metadata, dict):
                raw = metadata.get(BROWSER_URL_KEY)
                url = raw if isinstance(raw, str) else ""
            if not url:
                continue
            title = entry.get("title")
            carried.append(Game(
                app_id=0,
                store="luna",
                store_game_id=game_id,
                title=title if isinstance(title, str) and title else game_id,
                installed=False,
                tags=[GameTag.BROWSER, LUNA_STORE_TAG],
                metadata={BROWSER_URL_KEY: url},
            ))
        return carried
