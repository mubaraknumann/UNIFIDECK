"""Pure DOM-scrape parsing for the Luna catalog (task 8.3/8.4, Req 3.7/4.x).

Everything here is pure and deterministic — no I/O, no selector knowledge. The
DOM-shape assumptions live only in the injected JS
(:data:`~unifideck.stores.luna.luna_catalog.scrape_source._LIBRARY_SCRAPE_EXPRESSION`);
these functions take the already-normalised card list that query returns and
turn it into :class:`~unifideck.stores.luna.luna_catalog.models.LunaTitle` /
:class:`~unifideck.stores.luna.luna_catalog.models.EntitlementSet`, so task 8.4
can unit-test them against HTML fixtures without a browser.

:class:`LunaCatalogScrapeError` lives here (not with the driver) so both the
pure extractor and the impure
:class:`~unifideck.stores.luna.luna_catalog.scrape_source.DomScrapeCatalogSource`
can raise and import the one error type.
"""
from __future__ import annotations

from unifideck.stores.luna.luna_catalog.models import EntitlementSet, LunaTitle

#: The German Prime-inclusion marker. Best-effort: the spike saw it on the
#: detail page, not reliably on browse tiles, so a browse tile carrying it
#: is treated as entitled but its absence is NOT taken as "not entitled"
#: (Req 3.7 caveat).
PRIME_INCLUDED_MARKER = "In Prime enthalten"


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
