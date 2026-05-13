"""Browser-based GOG OAuth flow.

OP-22-gog-auth | py_modules/unifideck/stores/gog/auth.py

GOG uses a fairly standard OAuth2 authorization-
code flow. The user is shown the GOG login page in
a browser; the browser monitor catches the
``code=`` parameter on redirect; we exchange it
for tokens.

GOG-specifics:

* ``layout=client2`` query parameter — selects the
  Galaxy client login layout (compact form, no
  ads). Without it the user sees the full
  web-store layout.
* Logout requires explicit cookie wipe — GOG's
  session cookie persists across browser
  instances; not wiping it means the next
  ``start_auth`` silently re-uses the previous
  session.
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any

from ...auth.orchestrator import AuthOrchestrator
from ...core.types import AuthResult, Events, Result
from ...event_bus.event_bus import EventBus
from ...security import audit_auth_flow
from .config import GOG_AUTH_URL_FILE, GOGConfig
from .tokens import GOGTokenManager

logger = logging.getLogger(__name__)

_GOG_COOKIE_DOMAIN = "gog.com"


class GOGBrowserAuth:
    """Wraps ``AuthOrchestrator`` with GOG-specific URL + code exchange.

    Dependencies (bus + orch + tokens + config)
    are injected at construction. Same pattern as
    every other store's auth class.
    """

    def __init__(
        self,
        bus: EventBus,
        orchestrator: AuthOrchestrator,
        tokens: GOGTokenManager,
        config: GOGConfig,
    ) -> None:
        """Stash injected services.

        Args:
            bus: event bus (for STORE_LOGOUT).
            orchestrator: ``AuthOrchestrator``.
            tokens: ``GOGTokenManager`` for
                code exchange + clear.
            config: parsed ``GOGConfig``.
        """
        self._bus = bus
        self._orch = orchestrator
        self._tokens = tokens
        self._config = config

    @audit_auth_flow(store="gog", method="oauth_browser")
    async def start_auth(self) -> AuthResult:
        """Begin the OAuth flow through the orchestrator.

        Pre-validates config; missing fields →
        ``config_invalid``. Writes the auth URL to
        a file so the launcher dispatcher can pick
        it up if Steam restarts mid-flow.

        Returns:
            ``AuthResult``.
        """
        if not self._config.is_valid():
            return AuthResult(
                success=False,
                error="config_invalid",
                store="gog",
            )
        return await self._orch.run_flow(
            get_url=self._build_auth_url,
            allowed_uris=list(
                self._config.allowed_redirect_uris,
            ),
            exchange_code=self._exchange_code,
            background=True,
            write_url_file=GOG_AUTH_URL_FILE,
        )

    async def _build_auth_url(self) -> str:
        """Construct the GOG authorize URL.

        Adds ``layout=client2`` so users get the
        compact Galaxy login form, not the full
        web-store layout. ``safe="/: "`` keeps
        slashes and spaces unescaped per GOG's
        requirements.

        Returns:
            Full OAuth URL string.
        """
        params = {
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "response_type": "code",
            "layout": "client2",
        }
        query = urllib.parse.urlencode(params, safe="/: ")
        url = f"{self._config.auth_url}?{query}"
        logger.info("[GOGBrowserAuth] built OAuth URL")
        return url

    async def _exchange_code(self, code: str) -> AuthResult:
        """Hand the captured OAuth code to the token manager.

        Args:
            code: authorization code from the
                redirect URI.

        Returns:
            Success or ``token_exchange_failed``.
        """
        ok = await self._tokens.exchange_code(code)
        if ok:
            logger.info(
                "[GOGBrowserAuth] token exchange successful",
            )
            return AuthResult(success=True, store="gog")
        return AuthResult(
            success=False,
            error="token_exchange_failed",
            store="gog",
        )

    async def logout(self, browser_monitor: Any | None = None) -> Result:
        """Cancel any pending auth, wipe tokens, clear GOG cookies, emit STORE_LOGOUT.

        Cookie wipe is necessary because GOG's
        session cookie outlives the browser
        instance — without it, the next
        ``start_auth`` silently re-uses the
        old session.

        If ``browser_monitor`` is ``None`` the
        cookie wipe is skipped (still emits
        STORE_LOGOUT so the rest of the plugin
        sees the logout).

        Args:
            browser_monitor: optional
                ``OAuthBrowserMonitor`` for cookie
                wipe.

        Returns:
            ``Result(success=True)``.
        """
        self._orch.cancel_background()
        await self._tokens.clear()
        if browser_monitor is not None:
            try:
                await browser_monitor.clear_cookies_for_domain(
                    _GOG_COOKIE_DOMAIN,
                )
                logger.info(
                    "[GOGBrowserAuth] cleared cookies for %s",
                    _GOG_COOKIE_DOMAIN,
                )
            except Exception as e:
                logger.warning(
                    "[GOGBrowserAuth] cookie clear failed: %s",
                    e,
                )
        await self._bus.emit(Events.STORE_LOGOUT, store="gog")
        return Result(success=True)
