"""Catalog writer + library-cache reconciliation (task 8.5, Req 3/4).

:class:`LunaCatalog` is the orchestration seam the store's ``get_library``
calls. It runs the subscription gate (:class:`LunaSubscription`), retrieves the
title list over the SAME selected
:class:`~unifideck.stores.luna.luna_catalog.models.CatalogSource` strategy,
builds one browser-game :class:`Game` per VALID title, and reconciles that
against the Luna entries already in ``library_cache.json`` so the store returns
the authoritative value its sync layer expects.

The return contract matches ``SyncService`` reconciliation (and mirrors
``MicrosoftStore.get_library``): a ``list[Game]`` is AUTHORITATIVE (the sync
layer replaces every Luna shortcut with it, Req 4.8); an EMPTY list removes all
Luna entries (Req 4.9); ``None`` means "could not read — keep what is there"
(Req 4.2, 4.6, 3.6).

Malformed titles (missing ``luna_product_id``) are already dropped by
:func:`parse_library_cards`, so to honour Req 4.7 ("preserve the corresponding
existing entry") the writer carries the pre-existing entries forward rather
than letting the authoritative replace drop them.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from unifideck.core.types import Game, GameTag
from unifideck.launcher.browser_games import BROWSER_URL_KEY
from unifideck.stores.luna.luna_catalog.models import LUNA_STORE_TAG, LunaTitle
from unifideck.stores.luna.luna_catalog.parsing import compose_detail_url

if TYPE_CHECKING:
    from unifideck.auth.edge_browser.cdp_client import EdgeCDPClient
    from unifideck.stores.luna.luna_catalog.models import CatalogSource
    from unifideck.stores.luna.luna_config import LunaConfig
    from unifideck.stores.luna.luna_subscription import LunaSubscription

logger = logging.getLogger(__name__)


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
            game = LunaCatalog._reconstruct_carried_entry(entry, produced_ids)
            if game is not None:
                carried.append(game)
        return carried

    @staticmethod
    def _reconstruct_carried_entry(
        entry: object, produced_ids: set[str],
    ) -> Game | None:
        """Rebuild a carried-forward :class:`Game` from a previous entry.

        Returns ``None`` when the entry is malformed, already produced this
        run, or missing the data needed to reconstruct it.
        """
        if not isinstance(entry, dict):
            return None
        game_id = entry.get("store_game_id")
        if not isinstance(game_id, str) or not game_id:
            return None
        if game_id in produced_ids:
            return None
        metadata = entry.get("metadata")
        url = ""
        if isinstance(metadata, dict):
            raw = metadata.get(BROWSER_URL_KEY)
            url = raw if isinstance(raw, str) else ""
        if not url:
            return None
        title = entry.get("title")
        return Game(
            app_id=0,
            store="luna",
            store_game_id=game_id,
            title=title if isinstance(title, str) and title else game_id,
            installed=False,
            tags=[GameTag.BROWSER, LUNA_STORE_TAG],
            metadata={BROWSER_URL_KEY: url},
        )
