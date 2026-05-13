"""GOG OAuth code/refresh exchange + user-info fetch.

OP-22-gog-tokens-oauth | py_modules/unifideck/stores/gog/tokens/oauth.py

Pure OAuth client for the GOG endpoints. No state
held here — receives tokens via arguments, hands
results back via a save callback.

Quirk worth noting: GOG's token endpoint is
called with query-string params (not POST body),
which is why we build the URL inline rather than
using a regular ``urllib.urlopen`` with form data.
``fetch_json_get`` handles the actual HTTP.

The token age threshold is read from
``config.token_refresh_threshold_seconds`` —
default 40 minutes, well under GOG's 1-hour
access-token TTL.
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import TYPE_CHECKING, Any

from ..http import fetch_json_get
from .user_info import GOGUserInfo

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ..config import GOGConfig

    SaveCallback = Callable[[str, str], Awaitable[bool]]

logger = logging.getLogger(__name__)


class _TokenOAuth:
    """OAuth code/refresh client — pure HTTP, persistence via callback.

    Underscore prefix → internal class, consumers
    use ``GOGTokenManager`` which owns one of
    these. Persistence is decoupled via the
    ``save_callback``: this class never touches
    disk.
    """

    def __init__(
        self,
        *,
        config: GOGConfig,
        save_callback: SaveCallback,
    ) -> None:
        """Stash config and the save-callback.

        Args:
            config: ``GOGConfig`` for the
                OAuth endpoints + client_id/secret.
            save_callback: async callable taking
                ``(access, refresh)`` that the
                manager hooks up to its storage.
        """
        self._config = config
        self._save = save_callback

    async def exchange_code(self, auth_code: str) -> bool:
        """Trade an authorization code for tokens.

        Standard OAuth2 authorization_code grant —
        sends client_id, client_secret, code,
        redirect_uri. GOG's endpoint accepts these
        as query params.

        Args:
            auth_code: code from the OAuth
                redirect.

        Returns:
            True on successful save.
        """
        params = {
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": self._config.redirect_uri,
        }
        return await self._token_request(params)

    async def refresh_if_stale(
        self,
        *,
        access_token: str | None,
        refresh_token: str | None,
        age_seconds: float,
    ) -> bool:
        """Refresh the access token iff the access token is older than the threshold.

        Three paths:

        * Fresh + access token present → return
          True (no work);
        * Stale but no refresh token → return
          False (session is dead, caller should
          clear);
        * Stale + refresh token → POST refresh
          request, save result.

        Args:
            access_token: current access (may be
                ``None`` if never set).
            refresh_token: current refresh.
            age_seconds: seconds since last
                refresh.

        Returns:
            True iff after this call the access
            token is fresh.
        """
        threshold = self._config.token_refresh_threshold_seconds
        if age_seconds < threshold and access_token:
            return True
        if not refresh_token:
            logger.info(
                "[GOGTokens] no refresh token — session is dead",
            )
            return False
        logger.info(
            "[GOGTokens] token age %.0fs ≥ %ds, refreshing",
            age_seconds,
            threshold,
        )
        params = {
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        return await self._token_request(params)

    async def fetch_user_info(
        self,
        access_token: str,
        fallback: GOGUserInfo,
    ) -> GOGUserInfo:
        """Fetch ``/userData.json`` and merge with the fallback.

        On any failure (non-dict response, network
        error, missing fields), returns the
        fallback as-is — calling this is best-
        effort UI enrichment, never a launch
        blocker.

        Args:
            access_token: bearer.
            fallback: existing user info; missing
                fields in the response fall back
                to its values.

        Returns:
            Possibly-updated ``GOGUserInfo``.
        """
        url = f"{self._config.base_url}/userData.json"
        data = await fetch_json_get(
            url,
            bearer=access_token,
            user_agent=self._config.user_agent,
            timeout=10.0,
            log_prefix="[GOGTokens] userData",
        )
        if not isinstance(data, dict):
            return fallback
        return GOGUserInfo(
            username=str(
                data.get("username", "") or fallback.username,
            ),
            galaxy_user_id=str(
                data.get("galaxyUserId", "") or fallback.galaxy_user_id,
            ),
        )

    async def _token_request(
        self,
        params: dict[str, str],
    ) -> bool:
        """Send a token-endpoint request and dispatch to ``save_callback``.

        Used for both ``authorization_code`` and
        ``refresh_token`` grants — the only thing
        that varies between them is the
        ``params`` dict.

        Validates that the response has both
        ``access_token`` and ``refresh_token``
        (GOG returns ``access_token`` only on bad
        requests sometimes). Logs at ERROR if
        either is missing.

        Args:
            params: form params (urlencoded into
                the URL).

        Returns:
            True on successful save.
        """
        url = f"{self._config.token_url}?{urllib.parse.urlencode(params)}"
        data = await fetch_json_get(
            url,
            user_agent=self._config.user_agent,
            timeout=15.0,
            log_prefix="[GOGTokens] token endpoint",
        )
        if not isinstance(data, dict):
            return False
        access = data.get("access_token")
        refresh = data.get("refresh_token")
        if not access or not refresh:
            logger.error(
                "[GOGTokens] token response missing tokens: keys=%s",
                list(data.keys()),
            )
            return False
        return await self._save(access, refresh)


_ = Any
