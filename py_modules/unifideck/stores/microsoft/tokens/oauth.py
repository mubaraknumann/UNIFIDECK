"""OAuth mixin — refresh-token lifecycle for the Microsoft OAuth access token."""

from __future__ import annotations
import asyncio
import logging
import time
from typing import TYPE_CHECKING
from ..microsoft_auth import http_post
if TYPE_CHECKING:
    from ..microsoft_config import MicrosoftConfig
logger = logging.getLogger(__name__)
class OAuthMixin:
    """OAuth refresh-token lifecycle for the Microsoft access token.

    Provides ``exchange_code`` (used at first sign-in) and
    ``refresh_if_stale`` (used before every privileged call).
    Both rely on ``_token_request`` for the shared HTTP +
    save sequence.
    """
    _ms_access_token: str | None
    _ms_refresh_token: str | None
    _token_saved_at: float
    _config: MicrosoftConfig
    async def exchange_code(self, auth_code: str) -> bool:
        """Exchange an OAuth authorization code for access + refresh tokens.

        Args:
            auth_code: OAuth code captured from the redirect URL.

        Returns:
            True iff the token endpoint returned a valid response.
        """
        return await self._token_request({
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "code": auth_code,
            "grant_type": "authorization_code",
            "scope": self._config.scope,
        })
    async def refresh_if_stale(self) -> bool:
        """Refresh the access token if it's older than the refresh threshold.

        No-op (returns True) when the token is still fresh.
        Returns False if a refresh is required but no refresh
        token is available (session dead).

        Returns:
            True iff the current access token is fresh enough
            to use after this call returns.
        """
        age = time.time() - self._token_saved_at
        threshold = self._config.token_refresh_threshold_seconds
        if age < threshold and self._ms_access_token:
            return True
        if not self._ms_refresh_token:
            logger.error(
                "[MicrosoftTokens] refresh needed but no "
                "refresh token available — session dead",
            )
            return False
        logger.info(
            "[MicrosoftTokens] refreshing access token "
            "(age=%.0fs)",
            age,
        )
        return await self._token_request({
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "refresh_token": self._ms_refresh_token,
            "grant_type": "refresh_token",
            "scope": self._config.scope,
        })

    async def _token_request(
        self, params: dict[str, str],
    ) -> bool:

        """POST to the token endpoint and update in-memory + on-disk tokens on success.

        Updates ``_ms_access_token`` and (if returned) the
        refresh token, sets ``_token_saved_at`` to now, and
        persists via ``self.save()``.

        Args:
            params: Form fields posted to the token endpoint
                (grant_type + scope + grant-specific keys).

        Returns:
            True iff the endpoint returned ``access_token``.
        """
        headers = {
            "Content-Type":
                "application/x-www-form-urlencoded",
        }
        try:
            token_data = await (
                asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: http_post(
                        self._config.token_url,
                        params, headers,
                    ),
                )
            )
        except Exception as e:
            logger.error(
                "[MicrosoftTokens] token HTTP failed: %s", e,
            )
            return False
        if (
            not isinstance(token_data, dict)
            or "access_token" not in token_data
        ):
            error = (token_data or {}).get("error", "unknown")
            logger.error(
                "[MicrosoftTokens] token endpoint rejected "
                "request: %s", error,
            )
            return False
        self._ms_access_token = token_data["access_token"]
        new_refresh = token_data.get("refresh_token")
        if new_refresh:
            self._ms_refresh_token = new_refresh
        self._token_saved_at = time.time()
        await self.save()
        return True