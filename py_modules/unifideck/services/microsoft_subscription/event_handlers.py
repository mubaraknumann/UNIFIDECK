"""Event-bus subscriptions for the subscription service — invalidate cache on logout / re-auth."""

from __future__ import annotations
import logging
from typing import Any
from ...core.types import Events
from ...event_bus.event_bus_devex import subscribe
logger = logging.getLogger(__name__)
class _EventHandlersMixin:
    """Event handlers mixin."""
    @subscribe(Events.STORE_LOGOUT)
    async def _on_logout(self, **kwargs: Any) -> None:
        """Handle ``STORE_LOGOUT`` — invalidate the cache when Microsoft logs out.

        No-ops for other stores' logout events.

        Args:
            **kwargs: Event payload (``store`` is the filter key).
        """
        if kwargs.get("store") != "microsoft":
            return
        await self.invalidate()
    @subscribe(Events.STORE_AUTH_COMPLETE)
    async def _on_auth_complete(self, **kwargs: Any) -> None:
        """Handle ``STORE_AUTH_COMPLETE`` — invalidate cache on Microsoft re-auth.

        No-ops for other stores' auth-complete events.

        Args:
            **kwargs: Event payload.
        """
        if kwargs.get("store") != "microsoft":
            return
        await self.invalidate()
    @subscribe(Events.ACCOUNT_SWITCHED)
    async def _on_account_switched(self, **kwargs: Any) -> None:
        """Handle ``ACCOUNT_SWITCHED`` — invalidate the cache.

        Fired whenever the active Steam user changes; the cached
        subscription state from the previous account is no longer
        valid.

        Args:
            **kwargs: Event payload (unused).
        """
        await self.invalidate()