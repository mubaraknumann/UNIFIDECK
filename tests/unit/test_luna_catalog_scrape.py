"""Strategy B DOM-scrape parser tests against Luna library HTML fixtures (task 8.4).

Strategy B (``luna_catalog.DomScrapeCatalogSource``) is split in two by design
so this task can test the parse half without a browser:

  * the DOM/selector assumptions live ONLY in the injected JS
    (``_LIBRARY_SCRAPE_EXPRESSION``), which turns rendered browse-view tiles
    into a normalised ``[{"product_id", "name", "prime"}, ...]`` list;
  * the PURE parser (``parse_library_cards``) turns that list into
    ``(list[LunaTitle], EntitlementSet)`` with no I/O and no selector knowledge.

The test harness bridges HTML -> cards with a stdlib ``html.parser`` extractor
(:class:`_LibraryTileExtractor`) that mirrors the production JS extraction
rule-for-rule (same ``game_tile`` selector, same ``game_tile_`` id-prefix strip,
same product-id prefix requirement, same name fallback chain, same Prime
marker, same dedup). The real captured/synthetic HTML fixtures under
``tests/fixtures/luna/`` are the input; the real ``parse_library_cards`` is the
code under test. When task 8.1 drops genuine browse HTML into those fixtures,
only the extractor (and the shipped JS) would need touching — the parser
contract these assertions pin stays the same.

Covers (Req 4.7, 3.7):

  * a representative library -> the expected ``LunaTitle`` list, with the Prime
    marker driving ``entitled`` and the entitlement set;
  * an empty library -> zero titles + empty entitlement set, NOT an error
    (Req 4.9), distinct from a scrape failure;
  * malformed/partial tiles (no id, non-product id, empty id) skipped and a
    duplicate tile deduped (Req 4.7).
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

from unifideck.stores.luna.luna_catalog import (
    EntitlementSet,
    LunaTitle,
    parse_library_cards,
)

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "luna"

# These three constants are the extraction contract the shipped JS encodes.
# Kept here verbatim so the harness tracks the production rule exactly.
_PRIME_MARKER = "In Prime enthalten"
_TILE_PREFIX = "game_tile_"
# Both Amazon id namespaces, mirroring the shipped JS regex: adg.product
# (most titles) and luna.product (Luna-native/newer titles, e.g. STAR WARS
# Galactic Racer) — the latter was dropped when only adg was matched.
_PRODUCT_ID_RE = re.compile(r"amzn1\.(?:adg|luna)\.product\.[0-9a-fA-F-]+")


class _LibraryTileExtractor(HTMLParser):
    """Extract normalised Luna browse-view cards from library HTML.

    A stdlib stand-in for the shipped ``_LIBRARY_SCRAPE_EXPRESSION`` JS: it
    walks the same ``game_tile`` tiles and emits the same
    ``{"product_id", "name", "prime"}`` dicts the browser evaluate returns, so
    the pure ``parse_library_cards`` is exercised on exactly the shape it sees
    in production — but driven from an HTML fixture rather than a live DOM.

    The selector matches the JS: a tile is an element with ``role="button"``
    carrying either ``data-test-id="game_tile"`` or an ``id`` starting
    ``game_tile_amzn1.adg.product.``. The sibling ``*_impression`` wrapper
    (``role`` != ``button``) is ignored. For each tile the product id is the
    ``id`` with the ``game_tile_`` prefix stripped and any trailing ``_suffix``
    dropped; the name falls back aria-label -> nested img alt -> text; the Prime
    flag is the marker text appearing anywhere inside the tile.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict[str, object]] = []
        self._depth = 0
        self._tile: dict[str, object] | None = None
        self._tile_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]],
    ) -> None:
        self._depth += 1
        attr = {k: (v or "") for k, v in attrs}

        if self._tile is not None:
            # Inside an open tile: pick up a nested img alt and any text.
            if tag == "img" and not self._tile["name"]:
                self._tile["name"] = attr.get("alt", "")
            return

        if attr.get("role") != "button":
            return
        tile_id = attr.get("id", "")
        is_tile = (
            attr.get("data-test-id") == "game_tile"
            or (tile_id.startswith(_TILE_PREFIX)
                and _PRODUCT_ID_RE.search(tile_id) is not None)
        )
        if not is_tile:
            return

        self._tile = {
            "raw_id": tile_id,
            "aria": attr.get("aria-label", ""),
            "name": "",
            "text": "",
        }
        self._tile_depth = self._depth

    def handle_endtag(self, tag: str) -> None:
        if self._tile is not None and self._depth == self._tile_depth:
            self.cards.append(self._finalise(self._tile))
            self._tile = None
        self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._tile is not None:
            self._tile["text"] = str(self._tile["text"]) + data

    @staticmethod
    def _finalise(tile: dict[str, object]) -> dict[str, object]:
        """Turn one raw tile into a ``{"product_id", "name", "prime"}`` card.

        Mirrors the JS: match the product id (adg OR luna namespace) anywhere
        in the tile ``id`` via the shared regex (which stops at the UUID, so a
        trailing ``_suffix`` like ``_impression`` is dropped), choose the name
        by aria-label -> img alt -> text, and flag the Prime marker. A tile
        whose id carries no product id yields an empty ``product_id`` so the
        parser classifies it as malformed and skips it.
        """
        raw_id = str(tile["raw_id"])
        product_id = ""
        if raw_id.startswith(_TILE_PREFIX):
            m = _PRODUCT_ID_RE.search(raw_id)
            product_id = m.group(0) if m else ""
        text = str(tile["text"]).strip()
        name = str(tile["aria"]) or str(tile["name"]) or text or product_id
        return {
            "product_id": product_id,
            "name": name.strip(),
            "prime": _PRIME_MARKER in text,
        }


def _cards_from_fixture(name: str) -> list[dict[str, object]]:
    """Extract the normalised card list from a library HTML fixture.

    Dedups on ``product_id`` the way the shipped JS does (its ``seen`` set):
    the ``*_impression`` wrappers and repeated tiles collapse to one card per
    product id before the pure parser ever sees them. Cards with no product id
    (malformed) are kept so the parser is the thing that skips them.
    """
    html = (_FIXTURES / name).read_text(encoding="utf-8")
    extractor = _LibraryTileExtractor()
    extractor.feed(html)
    extractor.close()

    seen: set[str] = set()
    deduped: list[dict[str, object]] = []
    for card in extractor.cards:
        product_id = str(card["product_id"])
        if product_id and product_id in seen:
            continue
        if product_id:
            seen.add(product_id)
        deduped.append(card)
    return deduped


def test_representative_library_parses_to_expected_titles() -> None:
    """A representative library HTML yields the expected LunaTitle list.

    The Prime-included tile is entitled and in the entitlement set; a tile
    without the marker is not flagged (Req 3.7); the name falls back to
    aria-label when there is no nested img. The entitlement set is always
    stamped ``source="dom-scrape"`` (Req 4.1).

    Validates: Requirements 3.7, 4.7
    """
    cards = _cards_from_fixture("library_representative.html")

    titles, entitlements = parse_library_cards(cards)

    # Every tile on the ``my_games`` browse view is the account's own
    # playable library, so each is entitled regardless of the Prime marker
    # (the marker is unreliable on browse tiles — the real-device finding).
    assert titles == [
        LunaTitle(
            luna_product_id="amzn1.adg.product.11111111-1111-4111-8111-111111111111",
            name="Fortnite",
            entitled=True,
        ),
        LunaTitle(
            luna_product_id="amzn1.adg.product.22222222-2222-4222-8222-222222222222",
            name="Devil May Cry 5",
            entitled=True,
        ),
        LunaTitle(
            luna_product_id="amzn1.adg.product.33333333-3333-4333-8333-333333333333",
            name="Immortals Fenyx Rising",
            entitled=True,
        ),
        LunaTitle(
            luna_product_id="amzn1.luna.product.44444444-4444-4444-8444-444444444444",
            name="STAR WARS: Galactic Racer",
            entitled=True,
        ),
    ]
    assert entitlements == EntitlementSet(
        entitlements=(
            "amzn1.adg.product.11111111-1111-4111-8111-111111111111",
            "amzn1.adg.product.22222222-2222-4222-8222-222222222222",
            "amzn1.adg.product.33333333-3333-4333-8333-333333333333",
            "amzn1.luna.product.44444444-4444-4444-8444-444444444444",
        ),
        source="dom-scrape",
    )


def test_empty_library_yields_no_titles_without_error() -> None:
    """An empty library HTML yields zero titles + an empty set, not an error.

    "Zero titles" is a legitimate result (Req 4.9), distinct from a scrape
    failure: the parser returns an empty list and an empty ``dom-scrape``
    entitlement set rather than raising.

    Validates: Requirements 3.7, 4.7
    """
    cards = _cards_from_fixture("library_empty.html")
    assert cards == []

    titles, entitlements = parse_library_cards(cards)

    assert titles == []
    assert entitlements == EntitlementSet(entitlements=(), source="dom-scrape")


def test_malformed_tiles_are_skipped_and_duplicates_deduped() -> None:
    """Malformed/partial tiles are skipped and a duplicate tile is deduped.

    The fixture mixes one valid tile with: a tile with no id, a tile whose id
    is not an ``amzn1.adg.product`` id, a tile with an empty product id, and a
    duplicate of the valid tile. Only the single well-formed title survives,
    once (Req 4.7). The product id is the single required id — a card missing
    it cannot build a detail URL and is dropped.

    Validates: Requirements 4.7, 3.7
    """
    cards = _cards_from_fixture("library_malformed.html")

    titles, entitlements = parse_library_cards(cards)

    assert titles == [
        LunaTitle(
            luna_product_id="amzn1.adg.product.aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            name="Celeste",
            entitled=True,
        ),
    ]
    assert entitlements == EntitlementSet(
        entitlements=("amzn1.adg.product.aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",),
        source="dom-scrape",
    )


def test_parser_tolerates_a_non_list_scrape_result() -> None:
    """A non-list scrape value is treated as "no titles", never a raise.

    The pure parser stays total even if the driver ever hands it a malformed
    value: it returns zero titles + an empty ``dom-scrape`` set rather than
    raising (the driver, not the parser, signals a real scrape failure).

    Validates: Requirements 4.7
    """
    titles, entitlements = parse_library_cards(None)

    assert titles == []
    assert entitlements == EntitlementSet(entitlements=(), source="dom-scrape")
