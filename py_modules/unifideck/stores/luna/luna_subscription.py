"""LunaSubscription: determines the connected account's Luna entitlement
state over the selected catalog :class:`CatalogSource` strategy, and maps the
result onto the distinct Req 3 outcome states the store surfaces.

This is the lowest-confidence, highest-risk part of the Luna feature (design
Open Question Q4): the feasibility test exercised **no** Luna entitlement API,
so the data source is the pluggable :class:`CatalogSource` strategy from
:mod:`unifideck.stores.luna.luna_catalog` (default Strategy B, the DOM scrape).
``LunaSubscription`` stays strategy-agnostic: it depends only on the
:meth:`CatalogSource.fetch_entitlements` boundary, so the Req 3 outcome mapping
below holds identically whether the entitlements came from an API or a scrape
(design "Selection and risk handling"; task 8.7 contract test).

The outcome mapping (design "Error Handling" table, Req 3.2-3.6):

* **zero entitlements** (a real :class:`EntitlementSet` whose ``entitlements``
  is empty) → :attr:`SubscriptionOutcome.SUBSCRIPTION_REQUIRED` (Req 3.3, 3.4).
* **retrieval failure** (the fetch raised, or did not finish within the 30s
  budget, or the entitlement source was unreachable) →
  :attr:`SubscriptionOutcome.ENTITLEMENT_ERROR`, a state **distinct** from
  subscription-required (Req 3.2, 3.5). The previously determined set is
  **preserved unchanged** (Req 3.2).
* **no interface at all** (a set stamped ``source="none"``: no path could even
  attempt the determination) →
  :attr:`SubscriptionOutcome.ENTITLEMENT_SOURCE_UNAVAILABLE`, distinct from
  both of the above; the caller leaves previously synchronized library entries
  unchanged (Req 3.6).
* otherwise (one or more entitlements from a real source) →
  :attr:`SubscriptionOutcome.ENTITLED`.

Mirrors the *shape* of ``microsoft_subscription.py`` (a probe that returns a
frozen result dataclass carrying both the data and an ``ok``/error signal)
without copying its OAuth/gsToken mechanics, which do not apply to Luna.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from unifideck.stores.luna.luna_catalog import EntitlementSet

if TYPE_CHECKING:
    from unifideck.auth.edge_browser.cdp_client import EdgeCDPClient
    from unifideck.stores.luna.luna_catalog import CatalogSource
    from unifideck.stores.luna.luna_config import LunaConfig

logger = logging.getLogger(__name__)

#: The entitlement determination must complete within 30s (Req 3.1); a slower
#: determination is a retrieval failure (Req 3.2) that preserves the previous
#: set.
_ENTITLEMENT_TIMEOUT_S = 30.0


class SubscriptionOutcome(Enum):
    """The distinct Req 3 entitlement outcomes the store surfaces.

    Kept distinct (not collapsed to a bool) because the frontend renders a
    different prompt for each (design "Error Handling" table): a lapsed
    subscriber (:attr:`SUBSCRIPTION_REQUIRED`) is told to subscribe, a
    transient failure (:attr:`ENTITLEMENT_ERROR`) is offered a retry, and a
    missing interface (:attr:`ENTITLEMENT_SOURCE_UNAVAILABLE`) is a distinct
    "cannot determine" that must not look like "you are not subscribed".
    """

    #: One or more entitlements determined from a real source (Req 3.1, 3.7).
    ENTITLED = "entitled"
    #: A real source reported zero entitlements → subscription-required
    #: (Req 3.3, 3.4).
    SUBSCRIPTION_REQUIRED = "subscription_required"
    #: Retrieval failed / timed out / source unreachable (Req 3.2, 3.5),
    #: distinct from subscription-required; previous set preserved.
    ENTITLEMENT_ERROR = "entitlement_error"
    #: No entitlement interface could even be attempted (``source="none"``,
    #: Req 3.6), distinct from both of the above.
    ENTITLEMENT_SOURCE_UNAVAILABLE = "entitlement_source_unavailable"


@dataclass(frozen=True)
class SubscriptionResult:
    """The result of one entitlement determination.

    ``outcome`` is the mapped Req 3 state; ``entitlements`` is the set to carry
    forward. On success (:attr:`SubscriptionOutcome.ENTITLED` or
    :attr:`SUBSCRIPTION_REQUIRED`) it is the freshly determined set; on failure
    (:attr:`ENTITLEMENT_ERROR` / :attr:`ENTITLEMENT_SOURCE_UNAVAILABLE`) it is
    the previously determined set, preserved unchanged (Req 3.2). ``error``
    carries a short diagnostic string for the failure outcomes, ``None``
    otherwise.
    """

    outcome: SubscriptionOutcome
    entitlements: EntitlementSet
    error: str | None = None

    @property
    def is_entitled(self) -> bool:
        """Whether the account has at least one entitled title."""
        return self.outcome is SubscriptionOutcome.ENTITLED


class LunaSubscription:
    """Determines the account's Luna entitlement set over a catalog source.

    Holds the single source of truth for "which titles is this account
    entitled to?", mapped onto the distinct Req 3 outcomes. Strategy-agnostic:
    it drives only :meth:`CatalogSource.fetch_entitlements`, so the mapping is
    identical for an API or a DOM scrape (task 8.7 contract test).

    The last successfully determined :class:`EntitlementSet` is retained so a
    later failure can preserve it (Req 3.2) rather than regressing to "zero
    entitlements" (which would wrongly read as subscription-required).
    """

    def __init__(
        self,
        config: LunaConfig,
        source: CatalogSource,
        *,
        timeout_s: float = _ENTITLEMENT_TIMEOUT_S,
    ) -> None:
        """Build the subscription determiner.

        ``source`` is the selected :class:`CatalogSource` (default Strategy B,
        the DOM scrape). ``timeout_s`` is the determination budget (Req 3.1),
        injectable so tests need not wait the full 30s.
        """
        self._config = config
        self._source = source
        self._timeout_s = timeout_s
        #: The last set determined from a real source, carried forward on a
        #: later failure (Req 3.2). Starts empty with ``source="none"`` so a
        #: first-call failure preserves "nothing determined yet".
        self._last: EntitlementSet = EntitlementSet(
            entitlements=(), source="none",
        )

    @property
    def last_entitlements(self) -> EntitlementSet:
        """The most recently determined entitlement set (Req 3.2)."""
        return self._last

    async def determine(
        self, session: EdgeCDPClient,
    ) -> SubscriptionResult:
        """Determine the account's entitlements and map to a Req 3 outcome.

        Drives :meth:`CatalogSource.fetch_entitlements` under the 30s budget
        (Req 3.1) and maps the result:

        * fetch raises / times out → :attr:`ENTITLEMENT_ERROR`, previous set
          preserved (Req 3.2, 3.5);
        * ``source="none"`` → :attr:`ENTITLEMENT_SOURCE_UNAVAILABLE`, previous
          set preserved (Req 3.6);
        * empty entitlements from a real source → :attr:`SUBSCRIPTION_REQUIRED`
          (Req 3.3, 3.4), and the (empty) set is recorded as the new baseline;
        * otherwise → :attr:`ENTITLED` (Req 3.1, 3.7), set recorded.
        """
        try:
            entitlements = await asyncio.wait_for(
                self._source.fetch_entitlements(session, self._config),
                timeout=self._timeout_s,
            )
        except TimeoutError:
            logger.warning(
                "[LunaSubscription] entitlement determination exceeded %.0fs "
                "— preserving previous set (%d entitlement(s))",
                self._timeout_s, len(self._last.entitlements),
            )
            return SubscriptionResult(
                outcome=SubscriptionOutcome.ENTITLEMENT_ERROR,
                entitlements=self._last,
                error="timeout",
            )
        except Exception as exc:
            logger.warning(
                "[LunaSubscription] entitlement determination failed (%s: %s) "
                "— preserving previous set (%d entitlement(s))",
                type(exc).__name__, exc, len(self._last.entitlements),
            )
            return SubscriptionResult(
                outcome=SubscriptionOutcome.ENTITLEMENT_ERROR,
                entitlements=self._last,
                error=f"{type(exc).__name__}: {exc}",
            )
        return self._map_entitlements(entitlements)

    def _map_entitlements(
        self, entitlements: EntitlementSet,
    ) -> SubscriptionResult:
        """Map a successfully fetched :class:`EntitlementSet` to an outcome.

        Pure: distinguishes a ``source="none"`` set (no interface at all →
        :attr:`ENTITLEMENT_SOURCE_UNAVAILABLE`, Req 3.6) from an empty-but-real
        set (:attr:`SUBSCRIPTION_REQUIRED`, Req 3.4) from a populated one
        (:attr:`ENTITLED`). Only a real-source result updates the preserved
        baseline — a ``source="none"`` set never overwrites a previously
        determined set (Req 3.6).
        """
        if entitlements.source == "none":
            logger.info(
                "[LunaSubscription] no entitlement source available "
                "— preserving previous set (%d entitlement(s))",
                len(self._last.entitlements),
            )
            return SubscriptionResult(
                outcome=SubscriptionOutcome.ENTITLEMENT_SOURCE_UNAVAILABLE,
                entitlements=self._last,
                error="entitlement_source_unavailable",
            )
        # A real source answered: adopt it as the new baseline.
        self._last = entitlements
        if not entitlements.entitlements:
            logger.info(
                "[LunaSubscription] account has zero Luna entitlements "
                "(source=%s) — subscription required", entitlements.source,
            )
            return SubscriptionResult(
                outcome=SubscriptionOutcome.SUBSCRIPTION_REQUIRED,
                entitlements=entitlements,
            )
        logger.info(
            "[LunaSubscription] account entitled to %d Luna title(s) "
            "(source=%s)",
            len(entitlements.entitlements), entitlements.source,
        )
        return SubscriptionResult(
            outcome=SubscriptionOutcome.ENTITLED,
            entitlements=entitlements,
        )
