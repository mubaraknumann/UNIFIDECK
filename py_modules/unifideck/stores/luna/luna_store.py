"""LunaStore: the Amazon Luna cloud-streaming store orchestrator.

This is the Luna analogue of ``microsoft_store.py`` (xCloud). It subclasses
:class:`StoreBase` with the same ``(bus, cache, plugin_dir, config)``
constructor the auto-discovery registry instantiates, and composes the Luna
components implemented by the earlier tasks:

* :class:`LunaConfig` — the typed view over ``stores.luna`` config;
* :class:`LunaBrowser` — the thin config over the reused ``EdgeBrowser``;
* :class:`LunaSessionMonitor` — the cookie-store source of truth that
  **replaces** the Microsoft token manager (Luna has no tokens);
* :class:`LunaAuth` — drives the interactive Amazon sign-in;
* :class:`LunaSubscription` + :class:`LunaCatalog` over the default
  :class:`DomScrapeCatalogSource` strategy — the entitlement gate and the
  catalog→library-entry reconciliation;
* the spoofer / play-activator **factories** (not instances): both run in
  the launcher subprocess and are constructible from :class:`LunaConfig`
  alone, so the store only needs to hand their factory down.

The abstract-method surface (design "LunaStore" table):

| method                 | Luna behaviour                                            |
| ---------------------- | --------------------------------------------------------- |
| ``is_available``       | browser detected (``EdgeBrowser.is_installed``); else     |
|                        | needs-browser (Req 9.1)                                   |
| ``start_auth``         | ensure profile dir + flatpak override, launch at sign-in  |
|                        | with the spoofer active — delegates to ``LunaAuth``       |
| ``complete_auth``      | confirm cookies persisted via the session monitor         |
| ``logout``             | clear Amazon cookies in the Luna profile via              |
|                        | ``cookie_writer.py`` (through ``LunaSessionMonitor``)     |
| ``get_library``        | subscription gate → catalog → authoritative ``Game`` list |
| install/uninstall/     | reject — streaming-only (Req 13.6)                        |
| update/updates/size    |                                                           |

``Store_Status`` is reported to the frontend through the ``metadata`` payload
of the :class:`Result` / :class:`AuthResult` each operation returns, keyed
``store_status`` (mirroring how Microsoft surfaces ``needs_2fa`` etc.). The
full value set the design mandates lives in :class:`LunaStatus`; the coarse
``StoreStatus`` enum in ``core.types`` cannot express Luna's richer states
(``session_expired`` vs ``not_connected`` vs ``subscription_required`` …), so
Luna carries its own.

Region availability (Req 13.2/13.3), the gamepad Steam Input layout request
(Req 6.5/6.6), and the network/API retry + recovery loop (Req 11.1/11.2/
11.5/11.6) are **task 11.2**, implemented here:

* :meth:`LunaStore._check_region` runs during ``get_library`` after the
  spoofed browse page loads — a definitive region-unavailable answer within
  5s excludes every Luna title (returns ``[]``); an inconclusive check within
  10s keeps the existing entries (returns ``None``);
* :meth:`LunaStore.request_steam_input_layout` asks for a gamepad Steam Input
  layout within 5s of a Luna shortcut becoming the active launch target,
  reusing ``refresh_steam_controller_layout`` (the same bounce the xCloud
  path uses);
* :meth:`LunaStore._with_retry` retries a network/API operation up to 3 times
  behind a ``recovery_in_progress`` indicator, logs each failure with a
  timestamp + operation name + description within 1s, and restores the last
  known stable connection state before exposing a retry action.

``LunaStore`` is discoverable with no registry edit: ``StoreRegistry
.auto_discover`` matches the ``stores/luna/luna_store.py`` module and
instantiates the single ``StoreBase`` subclass exporting ``store_info`` —
exactly how Microsoft and the other stores register.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, TypeVar

from unifideck.auth.edge_browser import EdgeBrowser
from unifideck.core.types import (
    AuthResult,
    Events,
    Game,
    InstallResult,
    Result,
    StoreInfo,
)
from unifideck.stores.luna.luna_auth import LunaAuth
from unifideck.stores.luna.luna_browser import LunaBrowser
from unifideck.stores.luna.luna_catalog import (
    CatalogSyncStatus,
    DomScrapeCatalogSource,
    LunaCatalog,
)
from unifideck.stores.luna.luna_config import LunaConfig
from unifideck.stores.luna.luna_platform_spoofer import LunaPlatformSpoofer
from unifideck.stores.luna.luna_play_activator import LunaPlayActivator
from unifideck.stores.luna.luna_session_monitor import LunaSessionMonitor
from unifideck.stores.luna.luna_subscription import LunaSubscription
from unifideck.stores.shared.store_base import StoreBase

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.stores.luna.luna_catalog import CatalogSource

logger = logging.getLogger(__name__)

#: The store id used on every result this module returns.
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


class LunaStore(StoreBase):
    """Amazon Luna store (cloud-streaming only).

    Composition mirrors ``MicrosoftStore.__init__`` but swaps the token
    manager for :class:`LunaSessionMonitor` and adds the spoofer/activator
    **factories** instead of an OAuth subscription service. Every collaborator
    defaults to its real implementation and is injectable so the whole store
    is drivable in tests without a browser or a real config.
    """

    store_info = StoreInfo(
        name="luna",
        display_name="Amazon Luna",
        auth_method="browser-session",
        icon_asset="luna.png",
        supports_install=False,
    )

    def __init__(
        self,
        bus: EventBus,
        cache: CacheManager,
        plugin_dir: str | None = None,
        config: ConfigManager | None = None,
        browser: LunaBrowser | None = None,
        shortcut_service: Any = None,
        edge_browser: EdgeBrowser | None = None,
        session: LunaSessionMonitor | None = None,
        catalog_source: CatalogSource | None = None,
        auth: LunaAuth | None = None,
        spoofer_factory: Callable[[LunaConfig], LunaPlatformSpoofer] | None = None,
        activator_factory: Callable[[LunaConfig], LunaPlayActivator] | None = None,
    ) -> None:
        """Compose the Luna store over its collaborators (see module docstring)."""
        super().__init__(bus, cache, plugin_dir, config)
        self._luna_config: LunaConfig = LunaConfig.from_config_manager(config)
        logger.info("[LunaStore] %s", self._luna_config.describe())
        self._config_manager = config
        self._shortcut_service = shortcut_service
        self._edge = edge_browser or EdgeBrowser()
        self._browser = browser or LunaBrowser(self._luna_config, edge=self._edge)
        # LunaSessionMonitor replaces the Microsoft token manager: no tokens,
        # no refresh — "signed in" is non-expired Amazon cookies in the Luna
        # profile.
        self._session = session or LunaSessionMonitor(self._luna_config)
        # The catalog source is the SAME strategy the subscription gate and the
        # catalog reconciliation share (one choice drives both). Default is the
        # DOM scrape (Strategy B) — the only path the feasibility test proved.
        self._catalog_source: CatalogSource = (
            catalog_source or DomScrapeCatalogSource()
        )
        self._subscription = LunaSubscription(
            self._luna_config, self._catalog_source,
        )
        self._catalog = LunaCatalog(
            self._luna_config, self._subscription, self._catalog_source,
        )
        self._auth = auth or LunaAuth(
            bus, self._browser, self._session, self._luna_config,
        )
        # The spoofer/activator are constructed per launch in the launcher
        # subprocess; the store only carries their factories so a test can
        # swap them without a real CDP browser.
        self._spoofer_factory = spoofer_factory or LunaPlatformSpoofer
        self._activator_factory = activator_factory or LunaPlayActivator
        # The last status the store settled on while NOT retrying — the
        # "stable connection state" Req 11.1 restores to after a failed
        # operation. Starts at not_connected (no session proven yet) and is
        # advanced by every operation that reaches a terminal, non-recovery
        # outcome.
        self._last_stable_status: str = LunaStatus.NOT_CONNECTED

    # ── Availability ─────────────────────────────────────────────────

    async def is_available(self) -> bool:
        """Whether Luna is *connected* right now (Req 1.4).

        ``check_store_status`` renders this as the store's login badge, so it
        must mean "signed in", exactly as ``MicrosoftStore.is_available``
        does — not merely "a browser is installed". Luna is connected only
        when the streaming browser is present (the feature rides the CDP
        spoof, Req 9.1) **and** the Luna profile holds a usable Amazon
        session (:class:`LunaSessionMonitor`). The browser-installed-only
        gate stays available separately via :meth:`is_browser_installed`
        (the needs-browser install prompt) and ``start_auth``.
        """
        if not self._edge.is_installed:
            self._cached_available = False
            logger.info(
                "[LunaStore] no supported browser installed — needs_browser",
            )
            return False
        usable = self._session.is_usable()
        self._cached_available = usable
        if not usable:
            logger.info(
                "[LunaStore] browser present but no usable Luna session — "
                "not connected",
            )
        return usable

    # ── Auth ──────────────────────────────────────────────────────────

    async def start_auth(self, **kwargs: Any) -> AuthResult:
        """Prepare the interactive Luna sign-in (Req 1).

        In Gaming Mode a window from a process Steam did not launch has no
        gamescope session and never renders, so the actual kiosk sign-in runs
        in the ``unifideck-launcher`` process that Steam starts via a
        temporary shortcut (``UNIFIDECK_LUNA_ACTION=auth`` →
        ``launcher/flows/luna_auth.py``), exactly like the OAuth stores. This
        backend ``start`` therefore only *prepares* and reports ``pending``;
        it does **not** spawn the browser here (that would open an invisible
        second window). The frontend ``AuthDispatcher`` sees ``pending`` and
        launches the Luna auth shortcut.

        Needs-browser short-circuits before anything (Req 9.1). An already
        usable session reports connected immediately (no shortcut needed).
        """
        if not self._edge.is_installed:
            logger.info("[LunaStore] start_auth: browser not installed")
            return self._auth_result(
                success=False, error=LunaStatus.NEEDS_BROWSER,
                status=LunaStatus.NEEDS_BROWSER,
            )
        if self._session.is_usable():
            logger.info("[LunaStore] start_auth: already connected")
            return self._auth_result(
                success=True, status=LunaStatus.CONNECTED,
            )
        # Prepare only; the launcher's gamescope-visible flow does the sign-in.
        logger.info(
            "[LunaStore] start_auth: pending — sign-in runs via Steam shortcut",
        )
        return AuthResult(
            success=True, store=LUNA_STORE, tokens_cached=False,
            metadata={STATUS_KEY: LunaStatus.NOT_CONNECTED, "pending": True},
        )

    async def complete_auth(self, **kwargs: Any) -> AuthResult:
        """Confirm a usable Luna session exists (Req 1.3–1.6, 2).

        Unlike OAuth there is nothing to exchange: "complete" means the
        interactive sign-in left non-expired Amazon cookies in the Luna
        profile, which :class:`LunaSessionMonitor` is the source of truth for.
        A readable-but-empty/expired store is ``session_expired``; an
        unreadable store is ``session_unknown`` with a retry (Req 2.6).
        """
        state = self._session.read_session()
        if state.status == "usable":
            return self._auth_result(
                success=True, status=LunaStatus.CONNECTED,
            )
        status = (
            LunaStatus.SESSION_UNKNOWN if state.status == "unknown"
            else LunaStatus.SESSION_EXPIRED
        )
        return self._auth_result(
            success=False, error=status, status=status,
        )

    async def logout(self) -> Result:
        """Clear the Luna profile's Amazon cookies (Req 8.7–8.9).

        The kiosk is killed first (it owns the cookie DB), then the clear goes
        through :class:`LunaSessionMonitor`, which uses ``cookie_writer.py``.
        A clear failure retains the previously persisted session state and
        reports it (Req 8.8); success reports ``not_connected`` and emits
        ``STORE_LOGOUT`` so the frontend and sync layer react (Req 8.7).
        """
        try:
            self._browser.kill()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[LunaStore] browser kill during logout: %s", exc)
        try:
            cleared = self._session.clear_cookies()
        except Exception as exc:
            logger.warning("[LunaStore] logout clear failed: %s", exc)
            cleared = False
        if not cleared:
            return Result(
                success=False, store=LUNA_STORE, error="logout_failed",
                metadata={STATUS_KEY: LunaStatus.CONNECTED},
            )
        await self._bus.emit(Events.STORE_LOGOUT, store=LUNA_STORE)
        return Result(
            success=True, store=LUNA_STORE,
            metadata={STATUS_KEY: LunaStatus.NOT_CONNECTED},
        )

    # ── Library ─────────────────────────────────────────────────────────

    async def get_library(self, *, force: bool = False) -> list[Game] | None:
        """The entitled Luna titles, or ``None`` when they cannot be read.

        A ``list[Game]`` (including the empty list) is authoritative — the
        sync layer replaces every Luna shortcut with it — so every "could not
        read" path returns ``None`` to keep the existing shortcuts, exactly as
        ``MicrosoftStore.get_library`` does.

        Flow:
          1. The browser must be installed (else needs-browser) and a usable
             session must exist (else not-connected / session-expired).
          2. Open the Luna kiosk at the browse view with the spoofer attached
             so the DOM scrape sees the spoofed Windows identity.
          3. Run the subscription gate + catalog retrieval over the shared
             :class:`CatalogSource` and reconcile against the previous Luna
             library entries (:class:`LunaCatalog.sync`).

        The authoritative ``Game`` list (or ``None``) is returned; the catalog
        writer tags each title ``browser`` + ``luna`` with its composed detail
        URL in ``metadata["browser_url"]`` so the launcher resolves it to a
        ``stream`` target.
        """
        if not self._edge.is_installed:
            logger.info("[LunaStore] get_library: browser not installed")
            return None
        if not self._session.is_usable():
            logger.info(
                "[LunaStore] get_library: no usable Luna session; "
                "library not read",
            )
            return None

        try:
            return await self._with_retry(
                "get_library", self._sync_once,
            )
        except LunaOperationError:
            # Every attempt failed; the retry helper already restored the
            # last stable status and logged each failure. "Could not read"
            # keeps the existing Luna shortcuts (Req 4.2/4.6).
            return None

    async def _sync_once(self) -> list[Game] | None:
        """One catalog-sync attempt: open browser, region-gate, reconcile.

        Raises on a recoverable failure so :meth:`_with_retry` can retry;
        returns the authoritative ``list[Game]`` / ``None`` on a terminal
        outcome (including the region states, which are not retried — a
        region answer is definitive, not a transient fault).
        """
        cdp = await self._open_browser_for_sync()
        if cdp is None:
            msg = (
                "could not open the Luna browser for catalog sync "
                "(no browser / CDP never came up / spoof-config error)"
            )
            raise LunaOperationError(msg)
        try:
            region = await self._check_region(cdp)
            if region is not None:
                # A region answer short-circuits the catalog: either exclude
                # all titles (region_unavailable) or keep the existing
                # entries (availability_unknown). Neither is retried.
                games, status = region
                self._last_stable_status = status
                logger.info(
                    "[LunaStore] region gate -> status=%s, games=%s",
                    status, "None" if games is None else len(games),
                )
                return games
            previous = self._previous_luna_entries()
            result = await self._catalog.sync(cdp, previous)
        finally:
            await self._close_sync_browser()

        if result.status is CatalogSyncStatus.SYNC_ERROR:
            # A transport/parse failure of the catalog is exactly the
            # network/API fault Req 11.5 retries; surface it to the retry
            # helper rather than returning ``None`` on the first try.
            raise LunaOperationError(result.error or "catalog sync error")

        status = _SYNC_STATUS_TO_STORE_STATUS.get(
            result.status, LunaStatus.CONNECTED,
        )
        self._last_stable_status = status
        if result.status is CatalogSyncStatus.SUBSCRIPTION_REQUIRED:
            await self._bus.emit(
                Events.SYNC_SKIPPED, store=LUNA_STORE,
                reason=LunaStatus.SUBSCRIPTION_REQUIRED,
            )
        logger.info(
            "[LunaStore] catalog sync %s -> status=%s, games=%s",
            result.status.value, status,
            "None" if result.games is None else len(result.games),
        )
        # ``games`` is the authoritative value (list or None) already;
        # ``status`` is diagnostic and logged, surfaced to the panel by the
        # sync layer's own status read.
        return result.games

    # ── Region availability (Req 13.2, 13.3) ───────────────────────────

    async def _check_region(
        self, cdp: Any,
    ) -> tuple[list[Game] | None, str] | None:
        """Resolve whether Luna is available for the connected region.

        Returns:

        * ``None`` — the region is available (or the check is not wired):
          continue to the catalog sync;
        * ``([], region_unavailable)`` — Luna reported the region cannot
          stream; a definitive answer within 5s excludes **all** titles by
          returning the authoritative empty list (Req 13.2);
        * ``(None, availability_unknown)`` — the check did not complete /
          returned no result within 10s; keep the previously synchronized
          entries by returning ``None`` (Req 13.3).

        The check runs a single ``region.unavailable()`` probe over the same
        spoofed browse page the catalog scrape uses. The 5s budget bounds the
        definitive-answer path; an inconclusive probe (timeout, unreadable,
        error) is treated as availability-unknown once the 10s ceiling is hit.
        """
        start = time.monotonic()
        try:
            unavailable = await asyncio.wait_for(
                self._probe_region_unavailable(cdp),
                timeout=REGION_UNKNOWN_BUDGET_SECONDS,
            )
        except Exception as exc:  # any probe fault = availability unknown
            logger.warning(
                "[LunaStore] region check inconclusive within %.0fs (%s) — "
                "availability unknown, keeping existing entries",
                REGION_UNKNOWN_BUDGET_SECONDS, exc,
            )
            return (None, LunaStatus.AVAILABILITY_UNKNOWN)

        if unavailable is None:
            # Probe answered but could not decide: availability unknown.
            logger.info(
                "[LunaStore] region check returned no result — availability "
                "unknown, keeping existing entries",
            )
            return (None, LunaStatus.AVAILABILITY_UNKNOWN)

        if unavailable:
            elapsed = time.monotonic() - start
            if elapsed > REGION_UNAVAILABLE_BUDGET_SECONDS:
                # A definitive "unavailable" that took longer than the 5s
                # budget is reported as unknown instead, so the stricter
                # requirement (Req 13.2) is never silently violated.
                logger.warning(
                    "[LunaStore] region-unavailable answer took %.1fs (>%.0fs "
                    "budget) — reporting availability unknown",
                    elapsed, REGION_UNAVAILABLE_BUDGET_SECONDS,
                )
                return (None, LunaStatus.AVAILABILITY_UNKNOWN)
            logger.info(
                "[LunaStore] region unavailable for this account — excluding "
                "all Luna titles",
            )
            await self._bus.emit(
                Events.SYNC_SKIPPED, store=LUNA_STORE,
                reason=LunaStatus.REGION_UNAVAILABLE,
            )
            return ([], LunaStatus.REGION_UNAVAILABLE)

        return None

    async def _probe_region_unavailable(self, cdp: Any) -> bool | None:
        """Ask the loaded Luna page whether the region cannot stream.

        Returns ``True`` when Luna shows a region-unavailable state, ``False``
        when it is available, and ``None`` when the page cannot decide. The
        catalog source carries the one CDP-scrape seam, so the probe is
        delegated to it when it offers a ``region_unavailable`` check;
        otherwise region gating is treated as not-applicable (``False``) so a
        source without the probe never blocks the sync.
        """
        probe = getattr(self._catalog_source, "region_unavailable", None)
        if probe is None:
            return False
        result = await probe(cdp, self._luna_config)
        if result is None or isinstance(result, bool):
            return result
        return bool(result)

    # ── Steam Input layout request (Req 6.5, 6.6) ───────────────────────

    async def request_steam_input_layout(
        self, shortcut_appid: int, *, steam_port: int = _STEAM_CDP_PORT,
    ) -> bool:
        """Request a gamepad Steam Input layout for a Luna shortcut (Req 6.5).

        Called once a Luna Browser_Game_Entry's shortcut becomes the active
        launch target. Reuses ``refresh_steam_controller_layout`` — the same
        controller-popup bounce the xCloud path uses — to push the gamepad
        layout onto the shortcut, and bounds the whole request to 5s so a
        hung Steam CDP never blocks past the requirement (Req 6.5).

        Returns ``True`` when the layout bounce completed, ``False`` when it
        did not apply within the 5s budget (``refresh_steam_controller_layout``
        returned falsy, raised, or timed out). On a non-apply the caller
        surfaces the manual layout-selection guidance (Req 6.6); the method
        itself only reports whether automatic application completed.
        """
        if shortcut_appid <= 0:
            logger.warning(
                "[LunaStore] steam-input layout request skipped: invalid "
                "appid %r", shortcut_appid,
            )
            return False
        from unifideck.launcher.cdp.steam_controller_popup import (
            refresh_steam_controller_layout,
        )

        try:
            applied = await asyncio.wait_for(
                refresh_steam_controller_layout(
                    steam_port,
                    shortcut_appid,
                    # Fire immediately (no pre-delay): the 5s budget is the
                    # requirement window, not a thing to spend waiting.
                    delay=0.0,
                    dwell=2.5,
                ),
                timeout=STEAM_INPUT_LAYOUT_BUDGET_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "[LunaStore] gamepad Steam Input layout not applied within "
                "%.0fs for appid %s — surfacing manual layout steps (Req 6.6)",
                STEAM_INPUT_LAYOUT_BUDGET_SECONDS, shortcut_appid,
            )
            return False
        except Exception as exc:  # best-effort layout request
            logger.warning(
                "[LunaStore] gamepad Steam Input layout request failed for "
                "appid %s: %s — surfacing manual layout steps (Req 6.6)",
                shortcut_appid, exc,
            )
            return False

        if not applied:
            logger.warning(
                "[LunaStore] gamepad Steam Input layout did not apply for "
                "appid %s — surfacing manual layout steps (Req 6.6)",
                shortcut_appid,
            )
        else:
            logger.info(
                "[LunaStore] gamepad Steam Input layout requested for appid %s",
                shortcut_appid,
            )
        return bool(applied)

    # ── Error handling + recovery (Req 11.1, 11.2, 11.5, 11.6) ──────────

    async def _with_retry(
        self, operation: str, action: Callable[[], Awaitable[T]],
    ) -> T:
        """Run a network/API ``action`` with retry + a recovery indicator.

        Retries ``action`` up to 3 times after the first attempt (Req 11.5).
        While retrying, a ``recovery_in_progress`` indicator is emitted so the
        frontend can show recovery is underway (Req 11.6). Each failure is
        logged within 1s with a timestamp, the operation name, and an error
        description (Req 11.2). When every attempt fails, the last known
        stable connection state is restored and a retry action is exposed
        before :class:`LunaOperationError` is raised (Req 11.1).

        ``action`` signals a recoverable failure by raising; a successful
        attempt's return value is passed straight through.
        """
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                result = await action()
            except Exception as exc:  # uniform retry boundary
                last_error = exc
                self._log_operation_failure(operation, attempt, exc)
                if attempt <= MAX_RETRIES:
                    # Still retrying: show the recovery indicator and back off
                    # briefly before the next attempt (Req 11.6).
                    await self._emit_recovery_in_progress(operation, attempt)
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                # Retry budget exhausted: restore the last stable state and
                # expose a retry action (Req 11.1), then surface the failure.
                await self._restore_stable_state(operation)
                raise LunaOperationError(
                    f"{operation} failed after {MAX_ATTEMPTS} attempt(s): "
                    f"{type(exc).__name__}: {exc}",
                ) from exc
            else:
                return result
        # Unreachable: the loop either returns or raises. Keeps mypy happy.
        raise LunaOperationError(  # pragma: no cover - defensive
            f"{operation} failed: {last_error}",
        )

    def _log_operation_failure(
        self, operation: str, attempt: int, exc: Exception,
    ) -> None:
        """Log one network/API failure with a timestamp + op + description.

        Req 11.2 requires the record within 1s of the failure; the log call
        is synchronous and happens immediately on the failing attempt, so the
        line is written well inside the budget. The timestamp is emitted
        explicitly (not only via the log formatter) so the requirement holds
        regardless of how the launcher log is formatted.
        """
        logger.warning(
            "[LunaStore] %s | operation=%s attempt=%d/%d failed: %s: %s",
            time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            operation, attempt, MAX_ATTEMPTS,
            type(exc).__name__, exc,
        )

    async def _emit_recovery_in_progress(
        self, operation: str, attempt: int,
    ) -> None:
        """Signal the frontend that a recovery retry is underway (Req 11.6)."""
        try:
            await self._bus.emit(
                Events.SYNC_SKIPPED, store=LUNA_STORE,
                reason=LunaStatus.RECOVERY_IN_PROGRESS,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "[LunaStore] recovery-indicator emit failed (%s op=%s "
                "attempt=%d): %s", LunaStatus.RECOVERY_IN_PROGRESS,
                operation, attempt, exc,
            )

    async def _restore_stable_state(self, operation: str) -> None:
        """Restore + expose the last known stable state and a retry action.

        Req 11.1: after a failed operation, the store returns to the last
        connection state it was known to be in and exposes a retry action.
        The stable status is already tracked in ``_last_stable_status`` (set
        by every terminal, non-recovery outcome); here it is re-surfaced to
        the frontend so the panel leaves the recovery indicator and offers
        the retry.
        """
        logger.info(
            "[LunaStore] %s failed — restoring stable state %r with a retry "
            "action", operation, self._last_stable_status,
        )
        # ``SYNC_SKIPPED`` is the live, frontend-rendered status channel the
        # store already uses (see Microsoft's precedent): re-emitting the last
        # stable status here leaves the recovery indicator and lets the panel
        # offer the retry action, without inventing a second status channel.
        try:
            await self._bus.emit(
                Events.SYNC_SKIPPED, store=LUNA_STORE,
                reason=self._last_stable_status,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "[LunaStore] stable-state restore emit failed: %s", exc,
            )

    # ── Install lifecycle: refused, not faked (streaming-only, Req 13.6) ─
    #
    # Luna titles are cloud streams: nothing is downloaded, nothing lives on
    # disk, and the stream is always current. The three install hooks the ABC
    # requires therefore cannot be satisfied and say so with a visible
    # failure rather than a phantom ``success=True`` (the Microsoft store's
    # reasoning applies verbatim).
    _NOT_SUPPORTED = "not_supported"

    async def install_game(self, game_id: str, **kwargs: Any) -> InstallResult:
        """Refuse: Luna titles stream, so there is nothing to install."""
        logger.warning(
            "[LunaStore] refusing install for game_id=%s — Luna streams and "
            "has no local install", game_id,
        )
        return InstallResult(
            success=False, store=LUNA_STORE, game_id=game_id,
            install_path=None, error=self._NOT_SUPPORTED,
            error_code=self._NOT_SUPPORTED,
        )

    async def uninstall_game(self, game_id: str, **kwargs: Any) -> Result:
        """Refuse: nothing was installed, so nothing can be reclaimed."""
        return Result(
            success=False, store=LUNA_STORE,
            error=self._NOT_SUPPORTED, error_code=self._NOT_SUPPORTED,
        )

    async def update_game(self, game_id: str, **kwargs: Any) -> InstallResult:
        """Refuse: the stream is always current, so there is no update."""
        return InstallResult(
            success=False, store=LUNA_STORE, game_id=game_id,
            error=self._NOT_SUPPORTED, error_code=self._NOT_SUPPORTED,
        )

    async def check_for_updates(self) -> list[str]:
        """Streaming-only: there is never a local update to apply."""
        return []

    async def get_game_size(self, game_id: str) -> int | None:
        """Streaming-only: nothing is on disk, so the size is unknown."""
        return None

    # ── Sync-browser orchestration ──────────────────────────────────────

    async def _open_browser_for_sync(self) -> Any:
        """Open the Luna kiosk at the browse view with the spoofer attached.

        Returns a live :class:`EdgeCDPClient` once the kiosk's CDP port
        answers and the spoofer has attached browser-level, or ``None`` on any
        failure (no browser, CDP never came up, spoof-config error). The DOM
        scrape then runs against the spoofed browse page.
        """
        import asyncio

        from unifideck.auth.edge_browser.cdp_client import EdgeCDPClient
        from unifideck.stores.luna.luna_platform_spoofer import (
            LunaSpoofConfigError,
        )

        browse_url = self._browse_url()
        launched = await asyncio.to_thread(self._browser.launch, browse_url)
        if not launched:
            return None

        cdp = EdgeCDPClient(cdp_port=self._browser.cdp_port())
        if not await self._wait_for_cdp(cdp):
            await self._close_sync_browser()
            return None
        try:
            await self._spoofer_factory(self._luna_config).attach(cdp)
        except LunaSpoofConfigError as exc:
            logger.warning(
                "[LunaStore] sync spoofer attach failed (reported=%r): %s",
                exc.reported, exc,
            )
            await self._close_sync_browser()
            return None
        return cdp

    async def _wait_for_cdp(self, cdp: Any) -> bool:
        """Poll the kiosk's CDP port until it answers or the budget elapses."""
        import asyncio
        import time

        deadline = time.monotonic() + 20.0
        while True:
            if cdp.probe_cdp():
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.5)

    async def _close_sync_browser(self) -> None:
        """Terminate the sync kiosk (idempotent, best-effort)."""
        import asyncio

        try:
            await asyncio.to_thread(self._browser.kill)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[LunaStore] sync browser kill error: %s", exc)

    def _browse_url(self) -> str:
        """The Luna browse view the catalog scrape reads (Req 4.1).

        ``{luna_host}/browse?quick_search=my_games`` — the spike-verified
        view that renders the account's ``game_tile`` elements, not the
        recently-played shelf.
        """
        host = (self._luna_config.luna_host or "").strip().rstrip("/")
        if host and not host.startswith(("http://", "https://")):
            host = "https://" + host
        return f"{host}/browse?quick_search=my_games"

    def _previous_luna_entries(self) -> list[dict[str, object]]:
        """The Luna library entries from the last sync (for Req 4.7 preserve).

        Read from ``library_cache.json`` — the file the launcher also reads —
        so a malformed title this run does not drop a pre-existing entry. A
        missing/unreadable cache yields an empty list (nothing to preserve).
        """
        import json
        from pathlib import Path

        from unifideck.launcher.browser_games import LIBRARY_CACHE

        cache = Path(LIBRARY_CACHE).expanduser()
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        libraries = data.get("libraries") if isinstance(data, dict) else None
        entries = (libraries or {}).get(LUNA_STORE) if libraries else None
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict)]

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Plugin unload: make sure no Luna kiosk is left running."""
        try:
            self._browser.kill()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[LunaStore] shutdown browser kill error: %s", exc)

    # ── Status helpers ──────────────────────────────────────────────────

    @staticmethod
    def _auth_error_to_status(error: str | None) -> str:
        """Map a :class:`LunaAuth` error reason to a :class:`LunaStatus`.

        ``LunaAuth`` already uses the ``platform_spoofing_failure`` wire value
        for its spoof-config error; every other not-connected reason (early
        close, no cookies, timeout, profile permission/not-persisted, no
        browser) collapses to ``not_connected`` for the panel — the specific
        reason stays on ``AuthResult.error`` for diagnostics.
        """
        if error == LunaStatus.PLATFORM_SPOOFING_FAILURE:
            return LunaStatus.PLATFORM_SPOOFING_FAILURE
        return LunaStatus.NOT_CONNECTED

    def _auth_result(
        self, *, success: bool, status: str, error: str | None = None,
    ) -> AuthResult:
        """Build an :class:`AuthResult` carrying its :class:`LunaStatus`."""
        return AuthResult(
            success=success, store=LUNA_STORE, error=error,
            tokens_cached=success, metadata={STATUS_KEY: status},
        )

    @staticmethod
    def _set_status(result: AuthResult, status: str) -> None:
        """Stamp ``status`` onto an existing result's ``metadata`` payload."""
        result.metadata[STATUS_KEY] = status

    # ── Prerequisite install (reused flatpak installer) ─────────────────

    def is_browser_installed(self) -> bool:
        """Whether a supported streaming browser is installed (Req 9.1)."""
        return bool(self._edge.is_installed)

    async def install_browser(
        self,
        resume: Callable[[], Awaitable[T]] | None = None,
    ) -> Result:
        """Install the flatpak streaming browser and resume what was blocked.

        This is Luna's half of the Req 9 prerequisite flow. When
        :meth:`is_available` reported ``needs_browser`` (no supported browser,
        Req 9.1), the frontend offers the install and calls this to run it.
        The install itself reuses the shared flatpak installer unchanged
        (``EdgeBrowser.install`` → ``edge_browser/installer.py``) — Luna adds
        only the two things it specifically needs on top: its own
        narrowly-scoped profile-dir override plus ``/run/udev:ro`` after a
        successful install, and resuming the operation the missing browser
        interrupted.

        Outcomes (design error table, Req 9.4–9.6):

        * **flatpak absent** → ``Result(success=False, error=flatpak_not_found)``.
          Nothing is attempted; the pre-install state is untouched (Req 9.6).
        * **install failure or >600s** → ``Result(success=False,
          error=installation_failed)`` carrying the installer's reason in
          ``metadata["reason"]``; the interrupted operation is left in its
          pre-install state so the user can retry (Req 9.5). The reused
          installer makes no partial changes on failure, so "pre-install
          state kept" needs no extra rollback here.
        * **success within 600s** → the Luna profile-dir + ``/run/udev:ro``
          flatpak overrides are applied (Req 9.4), then — if a ``resume``
          callable was handed in — the interrupted Luna operation is run and
          its result is surfaced, so the user does not have to restart it
          manually. ``metadata["resumed"]`` records whether a resume ran.

        Args:
          resume: Optional zero-arg coroutine factory for the operation the
            missing browser interrupted (e.g. ``self.start_auth`` or
            ``self.get_library``). Run only after a successful install and
            after the overrides are applied. When omitted, a bare success is
            returned so a caller that only needs the browser present can use
            this without a follow-up.

        Returns:
          A :class:`Result`; on a resumed success its ``metadata`` also
          carries the resumed operation's own metadata (so the panel still
          sees the resulting :class:`LunaStatus`).
        """
        # Already present → nothing to install; resume straight away so a
        # racing second caller still gets its interrupted operation finished.
        if self._edge.is_installed:
            logger.info(
                "[LunaStore] install_browser: browser already installed",
            )
            return await self._apply_overrides_and_resume(resume)

        if not self._has_flatpak():
            logger.warning(
                "[LunaStore] install_browser: flatpak not available",
            )
            return Result(
                success=False, store=LUNA_STORE, error=FLATPAK_NOT_FOUND,
                error_code=FLATPAK_NOT_FOUND,
            )

        reason = await self._run_browser_install()
        if reason is not None:
            # Failure / timeout: the reused installer left the system in its
            # pre-install state (Req 9.5), so the interrupted operation is
            # simply not resumed and can be retried.
            logger.warning(
                "[LunaStore] install_browser failed: %s", reason,
            )
            return Result(
                success=False, store=LUNA_STORE, error=INSTALLATION_FAILED,
                error_code=INSTALLATION_FAILED, metadata={"reason": reason},
            )

        logger.info(
            "[LunaStore] install_browser succeeded — applying Luna overrides",
        )
        return await self._apply_overrides_and_resume(resume)

    async def _run_browser_install(self) -> str | None:
        """Run the reused flatpak install under the 600s ceiling (Req 9.5).

        Returns ``None`` on success, or a human-readable failure reason
        string (``flatpak-not-found`` is handled by the caller before this
        runs). A timeout past :data:`BROWSER_INSTALL_BUDGET_SECONDS` is
        reported as a reason so the caller maps it to ``installation-failed``.
        """
        try:
            raw = await asyncio.wait_for(
                self._edge.install(),
                timeout=BROWSER_INSTALL_BUDGET_SECONDS,
            )
        except TimeoutError:
            return (
                f"browser install did not complete within "
                f"{int(BROWSER_INSTALL_BUDGET_SECONDS)}s"
            )
        except Exception as exc:  # installer never raises, but be safe
            logger.warning("[LunaStore] browser install error: %s", exc)
            return f"{type(exc).__name__}: {exc}"
        if raw.get("success"):
            return None
        # The installer distinguishes a missing flatpak itself; surface its
        # reason verbatim so the frontend can show the specific message.
        return str(raw.get("error") or "browser install failed")

    async def _apply_overrides_and_resume(
        self, resume: Callable[[], Awaitable[T]] | None,
    ) -> Result:
        """Apply the Luna flatpak overrides, then resume the blocked op.

        The overrides are the two Req 9.4 requires after a successful install:
        the narrowly-scoped Luna profile-dir grant plus ``/run/udev:ro``, both
        applied through :class:`LunaProfileOverride` (which reuses the shared
        flatpak-override path and refuses to widen past the profile subtree).
        A native (non-flatpak) browser needs no override and the apply is a
        no-op there.
        """
        from unifideck.stores.luna.luna_profile_override import (
            LunaProfileOverride,
        )

        override = LunaProfileOverride.from_config(
            self._luna_config.profile_dir,
            self._luna_config.browser_candidates,
        )
        try:
            applied = await asyncio.to_thread(override.apply_flatpak_override)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "[LunaStore] applying Luna flatpak overrides failed: %s", exc,
            )
            applied = False
        if not applied:
            logger.warning(
                "[LunaStore] Luna flatpak overrides did not apply after "
                "install",
            )

        if resume is None:
            return Result(
                success=True, store=LUNA_STORE,
                metadata={"resumed": False, "overrides_applied": applied},
            )

        logger.info("[LunaStore] resuming the interrupted Luna operation")
        resumed = await resume()
        return self._result_from_resumed(resumed, overrides_applied=applied)

    @staticmethod
    def _result_from_resumed(resumed: Any, *, overrides_applied: bool) -> Result:
        """Fold the resumed operation's outcome into a prerequisite result.

        ``resume`` may be any Luna operation: a :class:`Result` /
        :class:`AuthResult` (``start_auth``), a ``list[Game] | None``
        (``get_library``), or something else. The success and any carried
        ``metadata`` (e.g. the resulting :class:`LunaStatus`) are propagated so
        the frontend sees the resumed operation's state, with
        ``metadata["resumed"]`` flagging that a resume ran.
        """
        if isinstance(resumed, Result):
            resumed.metadata.setdefault("resumed", True)
            resumed.metadata["overrides_applied"] = overrides_applied
            return resumed
        # ``get_library``-style resume: a list (even empty) means the browser
        # is now usable and the catalog was read; ``None`` means it still
        # could not be read.
        success = resumed is not None
        return Result(
            success=success, store=LUNA_STORE,
            metadata={"resumed": True, "overrides_applied": overrides_applied},
        )

    @staticmethod
    def _has_flatpak() -> bool:
        """Whether the ``flatpak`` CLI is on PATH (Req 9.6 gate)."""
        import shutil

        return shutil.which("flatpak") is not None
