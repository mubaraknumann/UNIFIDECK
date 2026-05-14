"""MicrosoftTokenManager — composes OAuth, persistence, and XBL chain mixins into a single facade."""

from __future__ import annotations
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING
from ....security import SecureTokenStore
from .oauth import OAuthMixin
from .persistence import PersistenceMixin
from .xbl_chain import XBLChainMixin
if TYPE_CHECKING:
    from ....event_bus.event_bus import EventBus
    from ..microsoft_config import MicrosoftConfig
logger = logging.getLogger(__name__)
class MicrosoftTokenManager(
    PersistenceMixin,
    OAuthMixin,
    XBLChainMixin,
):
    """Microsoft account token manager — OAuth, XBL chain, and persistence.

    Composes three mixins to provide the full lifecycle of
    Microsoft authentication tokens needed to talk to Xbox
    services: OAuth login + refresh (``OAuthMixin``), Xbox
    Live token + XSTS chain building (``XBLChainMixin``), and
    secure on-disk persistence via ``SecureTokenStore``
    (``PersistenceMixin``).
    """
    def __init__(
        self,
        config: MicrosoftConfig,
        locale_fn: Callable[[], str],
        secure_store: SecureTokenStore | None = None,
        bus: EventBus | None = None,
    ) -> None:
        """Wire dependencies and initialize empty token state.

        Args:
            config: Microsoft store config (endpoints, scopes,
                client IDs).
            locale_fn: Callable returning the current BCP-47
                locale (used as Accept-Language for auth requests).
            secure_store: Override the secure token store
                (default: a fresh ``SecureTokenStore`` bound to
                ``bus``).
            bus: Optional event bus; passed to the secure store
                for token-write notifications.
        """
        self._config = config
        self._locale_fn = locale_fn
        self._bus = bus
        self._secure_store = (
            secure_store or SecureTokenStore(bus=bus)
        )
        self._ms_access_token: str | None = None
        self._ms_refresh_token: str | None = None
        self._token_saved_at: float = 0.0
    @property
    def access_token(self) -> str | None:
        """Return the current Microsoft access token (if any).

        Returns:
            The access token string, or ``None`` if no auth chain
            has been built yet.
        """
        return self._ms_access_token
    @property
    def has_refresh_token(self) -> bool:
        """Indicate whether a refresh token is currently stored.

        Returns:
            True iff a non-empty refresh token is available.
        """
        return bool(self._ms_refresh_token)