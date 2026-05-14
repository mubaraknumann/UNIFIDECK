"""Browser-based Microsoft OAuth flow — drives Edge via AuthOrchestrator to capture the authorization code."""

from __future__ import annotations
import logging
import urllib.parse
from typing import Any
from ...auth.orchestrator import AuthOrchestrator
from ...core.types import AuthResult, Events, Result
from ...event_bus.event_bus import EventBus
from ...security import audit_auth_flow
from ...utils.locale import get_unifideck_locale
from .microsoft_config import MicrosoftConfig
from .tokens import MicrosoftTokenManager
logger = logging.getLogger(__name__)
_MS_AUTH_URL_FILE = "~/.local/share/unifideck/ms_auth_url.txt"
class MicrosoftBrowserAuth:
    """Microsoft OAuth flow driven through Edge + AuthOrchestrator.

    Builds the OAuth URL with the user's locale, hands it to
    the orchestrator (which launches Edge and watches for the
    redirect), and exchanges the captured code for tokens via
    ``MicrosoftTokenManager``.
    """
    def __init__(
    self,
    bus: EventBus,
    orchestrator: AuthOrchestrator,
    tokens: MicrosoftTokenManager,
    config: MicrosoftConfig,
    config_manager: Any,
    ) -> None:
        """Wire dependencies for the browser-driven Microsoft OAuth flow.

        Args:
            bus: Event bus.
            orchestrator: Auth orchestrator (drives the higher-level
                OAuth state machine).
            tokens: Microsoft token manager (receives the OAuth
                access + refresh tokens once captured).
            config: Microsoft store config (endpoints, scopes).
            config_manager: Plugin-wide ConfigManager.
        """
        self._bus = bus
        self._orch = orchestrator
        self._tokens = tokens
        self._config = config
        self._config_manager = config_manager
    @audit_auth_flow(store="microsoft", method="oauth_browser")
    async def start_auth(self) -> AuthResult:
        """Kick off the Microsoft OAuth flow in the background.

        Validates the config, then delegates to ``run_flow`` —
        which is responsible for launching Edge against the URL,
        watching for a redirect, capturing the code, and feeding
        it to ``_exchange_code``.

        Returns:
            ``AuthResult`` — ``config_invalid`` if the configured
            OAuth endpoints/scope are missing or empty.
        """
        if not self._config.is_valid():
            return AuthResult(
                success=False,
                error="config_invalid",
                store="microsoft",
            )
        return await self._orch.run_flow(
            get_url=self._build_auth_url,
            allowed_uris=list(
                self._config.allowed_redirect_uris,
            ),
            exchange_code=self._exchange_code,
            background=True,
            write_url_file=_MS_AUTH_URL_FILE,
        )
    async def _build_auth_url(self) -> str:
        """Build the Microsoft OAuth authorization URL with the user's locale.

        Returns:
            Authorization URL string (always with
            ``response_type=code`` and the configured scope).
        """
        locale = get_unifideck_locale(self._config_manager)
        params = {
        "client_id": self._config.client_id,
        "redirect_uri": self._config.redirect_uri,
        "response_type": "code",
        "scope": self._config.scope,
        "ui_locales": locale,
        }
        query = urllib.parse.urlencode(params, safe="/: ")
        url = f"{self._config.auth_url}?{query}"
        logger.info(
        "[MicrosoftBrowserAuth] built OAuth URL (locale=%s)",
        locale,
        )
        return url

    async def _exchange_code(self, code: str) -> AuthResult:

        """Exchange the captured OAuth code for an access + refresh token.

        Args:
            code: OAuth code from the redirect.

        Returns:
            ``AuthResult`` — ``token_exchange_failed`` if the
            token endpoint rejected the code.
        """
        ok = await self._tokens.exchange_code(code)
        if ok:
            logger.info(
                "[MicrosoftBrowserAuth] token exchange successful",
            )
            return AuthResult(success=True, store="microsoft")
        return AuthResult(
            success=False,
            error="token_exchange_failed",
            store="microsoft",
        )
    async def logout(self) -> Result:
        """Cancel any pending background flow, clear tokens, emit STORE_LOGOUT.

        Returns:
            ``Result`` (always success=True).
        """
        self._orch.cancel_background()
        await self._tokens.clear()
        await self._bus.emit(
        Events.STORE_LOGOUT, store="microsoft",
        )
        return Result(success=True)