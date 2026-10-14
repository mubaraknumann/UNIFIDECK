"""Library retrieval, region gating, and sync-browser orchestration.

The ``get_library`` flow for the Luna store (Req 3/4/13): open the spoofed
kiosk, run the region gate, and reconcile the catalog into the authoritative
``Game`` list. Split out of the former single ``luna_store.py`` as a mixin that
:class:`~unifideck.stores.luna.luna_store.LunaStore` composes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Events
from unifideck.stores.luna.luna_catalog import CatalogSyncStatus
from unifideck.stores.luna.luna_store_base import _LunaStoreState
from unifideck.stores.luna.luna_store_constants import (
    _SYNC_STATUS_TO_STORE_STATUS,
    LUNA_STORE,
    REGION_UNAVAILABLE_BUDGET_SECONDS,
    REGION_UNKNOWN_BUDGET_SECONDS,
    LunaOperationError,
    LunaStatus,
)

if TYPE_CHECKING:
    from unifideck.core.types import Game

logger = logging.getLogger(__name__)


class _LunaLibrarySyncMixin(_LunaStoreState):
    """``get_library`` + region gate + sync-browser orchestration."""

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
            games: list[Game] | None = await self._with_retry(
                "get_library", self._sync_once,
            )
        except LunaOperationError:
            # Every attempt failed; the retry helper already restored the
            # last stable status and logged each failure. "Could not read"
            # keeps the existing Luna shortcuts (Req 4.2/4.6).
            return None
        return games

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
