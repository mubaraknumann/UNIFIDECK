"""Strategy B — the CDP-driven DOM scrape catalog source (task 8.3).

This is the default, always-available catalog source: it drives the rendered
Luna library pages in the already-spoofed streaming browser over the SAME
``BrowserCdpSession`` abstraction / page-target-attach / ``Runtime.evaluate``
pattern established in :mod:`unifideck.stores.luna.luna_platform_spoofer` and
reused in :mod:`unifideck.stores.luna.luna_play_activator`. No undiscovered API
is needed — only the web UI the feasibility test proved works.

The two concerns are kept strictly separate: the impure CDP driving lives in
:class:`DomScrapeCatalogSource`; the DOM-shape assumptions live ONLY in the
injected JS (:data:`_LIBRARY_SCRAPE_EXPRESSION`), which returns an
already-normalised card list that the pure
:func:`~unifideck.stores.luna.luna_catalog.parsing.parse_library_cards` turns
into titles + an entitlement set.

╔═══════════════════════════════════════════════════════════════════╗
║ SPIKE-VERIFIED (F6, catalog/launch spike) — the browse-view tile    ║
║ structure below was confirmed live on a Steam Deck against          ║
║ luna.amazon.de/browse?quick_search=my_games (180 real tiles):       ║
║   • each title tile is div[data-test-id="game_tile"][role="button"] ║
║     with id="game_tile_amzn1.adg.product.<UUID>"                     ║
║   • the stable per-title id is the amzn1.adg.product.<UUID> product  ║
║     id (strip the "game_tile_" prefix) — there is NO slug and NO     ║
║     ASIN in the library DOM                                          ║
║   • the name is read from the tile aria-label / nested img alt       ║
║   • the launch URL is {luna_host}/game/{product_id}; Luna redirects  ║
║     it to the canonical slug+ASIN detail page                        ║
║ Still BEST-EFFORT and only live-probed (not saved as a fixture):     ║
║ the exact browse-tile NAME source and the Prime-inclusion marker —   ║
║ "In Prime enthalten" was seen on the DETAIL page, not reliably on    ║
║ browse tiles, so tile-level entitlement is unconfirmed. When task    ║
║ 8.4 saves real browse HTML, ONLY the JS extraction below should      ║
║ need touching — the pure parser and the CDP driver stay unchanged.   ║
╚═══════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from unifideck.stores.luna.luna_catalog.models import EntitlementSet, LunaTitle
from unifideck.stores.luna.luna_catalog.parsing import (
    LunaCatalogScrapeError,
    extract_cards_from_evaluate,
    parse_library_cards,
)
from unifideck.stores.luna.luna_cdp import attach_to_page_target

if TYPE_CHECKING:
    from collections.abc import Callable

    from unifideck.auth.edge_browser.cdp_client import EdgeCDPClient
    from unifideck.stores.luna.luna_config import LunaConfig
    from unifideck.stores.luna.luna_platform_spoofer import BrowserCdpSession

logger = logging.getLogger(__name__)

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
    "document.querySelectorAll("
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

    _session_factory: Callable[[str], BrowserCdpSession]

    def __init__(
        self,
        *,
        session_factory: Callable[[str], BrowserCdpSession] | None = None,
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
        self, session: BrowserCdpSession, session_id: str,
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

    async def _await_tiles(
        self, session: BrowserCdpSession, session_id: str,
    ) -> None:
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

    async def _tile_count(
        self, session: BrowserCdpSession, session_id: str,
    ) -> int:
        """Current rendered browse-view game-tile count (0 on any failure)."""
        result = await session.send(
            "Runtime.evaluate",
            {"expression": _TILE_COUNT_EXPRESSION, "returnByValue": True},
            session_id=session_id,
        )
        count = (((result or {}).get("result") or {}).get("value")) or 0
        return count if isinstance(count, int) else 0

    async def _attach_to_page_target(self, session: BrowserCdpSession) -> str:
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
