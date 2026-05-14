"""
Monitor the auth prefix for credential-file appearance — signals sign-in completion.

OP-58d | py_modules/unifideck/stores/ubisoft/auth/session_monitor.py

After the user is redirected to UPC for sign-in, we have no callback to
know when they've finished — UPC just writes credentials to disk and
exits. ``_AuthSessionMonitor`` polls the auth prefix for the appearance
of the canonical credential files (``ConnectSecureStorage.dat``,
``user.dat``) and signals completion through an ``asyncio.Event``.

Polling rate is moderate (~1 Hz) to avoid burning CPU during long
sign-in flows.
"""

from __future__ import annotations
import asyncio
import logging
from collections.abc import Callable
from typing import Any
from ....core.types import Result

_AUTH_MONITOR_TIMEOUT_S = 30 * 60
_AUTH_MONITOR_POLL_INTERVAL_S = 2.0
logger = logging.getLogger(__name__)


class _AuthSessionMonitor:
    """Background poller that signals sign-in completion.

    Polls the auth prefix every 2 s for the appearance of UPC's
    credential files. On capture, propagates credentials to all
    prefixes and queues the post-auth asset-ensure pass. Times
    out silently after 30 min.
    """

    def __init__(
        self,
        *,
        config: Any,
        session: Any,
        queue_auth_assets_ensure: Callable[[str], None],
    ) -> None:
        """Wire dependencies for the post-sign-in session monitor.

        Args:
            config: Ubisoft store config.
            session: Ubisoft session state to watch.
            queue_auth_assets_ensure: Callback enqueueing
                auth-assets propagation once a session is captured.
        """
        self._config = config
        self._session = session
        self._queue_auth_assets_ensure = queue_auth_assets_ensure
        self._monitor_task: asyncio.Task[None] | None = None
        self._session_captured = False

    async def start(self) -> Result:
        """Start (or restart) the background monitor task.

        Cancels and replaces any prior task so callers can re-arm the
        monitor without bookkeeping.

        Returns:
            A successful ``Result``.
        """
        if self._monitor_task is not None and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(
                    "[UbisoftAuth] old monitor task error on cancel: %s",
                    e,
                )
        self._session_captured = False
        self._monitor_task = asyncio.create_task(self._loop())
        logger.info(
            "[UbisoftAuth] started auth session monitor",
        )
        return Result(success=True)

    async def _loop(self) -> None:
        """Main monitor loop — poll until capture or timeout (30 min).

        On capture: invokes ``session.propagate_all_to_all`` and
        queues a post-capture asset-ensure pass. On timeout: logs
        a warning and returns.
        """
        auth_dir = self._config.auth_prefix_dir_expanded
        elapsed = 0.0
        while elapsed < _AUTH_MONITOR_TIMEOUT_S:
            await asyncio.sleep(_AUTH_MONITOR_POLL_INTERVAL_S)
            elapsed += _AUTH_MONITOR_POLL_INTERVAL_S
            captured = self._session.capture(auth_dir)
            if captured:
                logger.info(
                    "[UbisoftAuth] auth session monitor: token captured",
                )
                self._session.propagate_all_to_all()
                self._queue_auth_assets_ensure(
                    "post-auth-session-capture",
                )
                self._session_captured = True
                return
        logger.warning(
            "[UbisoftAuth] auth session monitor timed out after %ds",
            _AUTH_MONITOR_TIMEOUT_S,
        )

    def status(self) -> dict[str, Any]:
        """Return the current monitor state.

        Returns:
            Dict ``{captured, monitoring}`` — whether credentials
            have been captured at least once, and whether the
            monitor task is currently alive.
        """
        monitoring = self._monitor_task is not None and not self._monitor_task.done()
        return {
            "captured": self._session_captured,
            "monitoring": monitoring,
        }
