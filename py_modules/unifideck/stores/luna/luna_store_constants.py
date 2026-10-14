"""Shared constants and status vocabulary for the Luna store package.

Pulled out of the former single ``luna_store.py`` module so the store class
and its method mixins share one definition of the ``Store_Status`` values, the
retry/region/install budgets, and the sync-status mapping without a circular
import.
"""
from __future__ import annotations

from typing import TypeVar

from unifideck.stores.luna.luna_catalog import CatalogSyncStatus

#: The store id used on every result the Luna store returns.
LUNA_STORE = "luna"

#: The ``metadata`` key under which every Luna result carries its
#: :class:`LunaStatus` so the frontend can render the right panel state. The
#: frontend treats ``metadata`` as an optional hint (see ``Result``), so this
#: is additive and never breaks a caller that only reads ``success``.
STATUS_KEY = "store_status"

#: Error strings the browser-prerequisite install flow surfaces (Req 9). These
#: are ``Result.error`` outcome labels, not ``Store_Status`` panel states (see
#: the design error table): ``needs_browser`` is the panel state the frontend
#: reads to offer the install prompt, while these two name *why* an install
#: attempt did not produce a usable browser.
FLATPAK_NOT_FOUND = "flatpak_not_found"
INSTALLATION_FAILED = "installation_failed"

#: Upper bound on a single browser install (Req 9.4/9.5): a success must land
#: within 600s, and anything slower is reported as ``installation-failed`` with
#: the pre-install state kept. The reused flatpak installer carries its own
#: (shorter) per-step timeouts; this is the outer ceiling the requirement sets.
BROWSER_INSTALL_BUDGET_SECONDS = 600.0

#: How many attempts a network/API operation gets before the failure is
#: surfaced (Req 11.5): the first try plus up to 3 retries.
MAX_ATTEMPTS = 4

#: Max retries after the first attempt (Req 11.5 — "up to 3 times").
MAX_RETRIES = MAX_ATTEMPTS - 1

#: Backoff between retry attempts. Kept short so the whole retry budget stays
#: well inside the sync window and the recovery indicator is transient.
_RETRY_BACKOFF_SECONDS = 0.5

#: Region-availability budgets (Req 13.2/13.3): a definitive
#: region-unavailable answer must surface within 5s; an inconclusive check is
#: given until 10s before it is reported as availability-unknown.
REGION_UNAVAILABLE_BUDGET_SECONDS = 5.0
REGION_UNKNOWN_BUDGET_SECONDS = 10.0

#: The Steam CEF CDP port the controller-layout bounce drives (same default
#: the xCloud path uses — ``cdp.port``).
_STEAM_CDP_PORT = 8080

#: The gamepad Steam Input layout request must be issued within 5s of the
#: shortcut becoming the active launch target (Req 6.5). ``delay=0`` fires the
#: bounce immediately and the whole request is bounded by this budget so a
#: hung Steam CDP never blocks past the requirement.
STEAM_INPUT_LAYOUT_BUDGET_SECONDS = 5.0

T = TypeVar("T")


class LunaOperationError(Exception):
    """A Luna network/API operation that failed every retry (Req 11.5).

    Raised by :meth:`LunaStore._with_retry` once the first attempt and all
    retries have failed. The store's callers (e.g. ``get_library``) catch it
    and map it to their "could not read" result, after the retry helper has
    already restored the last stable state and logged each failure.
    """


class LunaStatus:
    """The full ``Store_Status`` value set Luna surfaces (design, Req 10/11/13).

    Not a ``core.types`` ``StrEnum`` because that package may not grow a
    store-specific vocabulary, and the coarse ``StoreStatus`` (available /
    not_authenticated / unavailable / error) cannot express the distinctions
    the Luna frontend needs (``session_expired`` is a *re-auth* prompt,
    ``not_connected`` is a *first sign-in* prompt, and both differ from
    ``subscription_required``). Kept as plain string constants so the store,
    the frontend mapping, and the tests agree on the exact wire values.
    """

    #: A usable Luna session exists (Req 1.4).
    CONNECTED = "connected"
    #: No usable session; an interactive Luna sign-in is required (Req 2.4).
    NOT_CONNECTED = "not_connected"
    #: Session cookies absent/expired, distinct from never-connected so the
    #: frontend offers re-authentication (Req 2.5).
    SESSION_EXPIRED = "session_expired"
    #: The cookie store could not be read in time; retry exposed (Req 2.6).
    SESSION_UNKNOWN = "session_unknown"
    #: No supported browser installed; prompt the flatpak install (Req 9.1).
    NEEDS_BROWSER = "needs_browser"
    #: The account has zero Luna entitlements (Req 3.4).
    SUBSCRIPTION_REQUIRED = "subscription_required"
    #: Entitlement retrieval failed, distinct from subscription-required
    #: (Req 3.5).
    ENTITLEMENT_ERROR = "entitlement_error"
    #: No entitlement interface could be reached at all (Req 3.6).
    ENTITLEMENT_SOURCE_UNAVAILABLE = "entitlement_source_unavailable"
    #: The connected account's region cannot stream Luna (task 11.2, Req 13).
    REGION_UNAVAILABLE = "region_unavailable"
    #: Region could not be determined; entries kept (task 11.2, Req 13).
    AVAILABILITY_UNKNOWN = "availability_unknown"
    #: The CDP platform spoof failed/verification mismatched (Req 11.4, 14.12).
    PLATFORM_SPOOFING_FAILURE = "platform_spoofing_failure"
    #: A streaming launch failed to reach the player (Req 5.5).
    LAUNCH_FAILURE = "launch_failure"
    #: A retry/recovery cycle is in progress (task 11.2, Req 11.6).
    RECOVERY_IN_PROGRESS = "recovery_in_progress"


#: Maps a :class:`CatalogSyncStatus` to the frontend-facing status string the
#: store surfaces after a catalog sync. ``SYNCED`` / ``SYNCED_ZERO`` /
#: ``SYNCED_WITH_MALFORMED`` are all "connected" states (the library was read
#: and is authoritative); the gate/error statuses carry their own distinct
#: value so the panel renders the matching prompt.
_SYNC_STATUS_TO_STORE_STATUS: dict[CatalogSyncStatus, str] = {
    CatalogSyncStatus.SYNCED: LunaStatus.CONNECTED,
    CatalogSyncStatus.SYNCED_ZERO: LunaStatus.CONNECTED,
    CatalogSyncStatus.SYNCED_WITH_MALFORMED: LunaStatus.CONNECTED,
    CatalogSyncStatus.SUBSCRIPTION_REQUIRED: LunaStatus.SUBSCRIPTION_REQUIRED,
    CatalogSyncStatus.ENTITLEMENT_ERROR: LunaStatus.ENTITLEMENT_ERROR,
    CatalogSyncStatus.ENTITLEMENT_SOURCE_UNAVAILABLE: (
        LunaStatus.ENTITLEMENT_SOURCE_UNAVAILABLE
    ),
    CatalogSyncStatus.SYNC_ERROR: LunaStatus.ENTITLEMENT_ERROR,
}
