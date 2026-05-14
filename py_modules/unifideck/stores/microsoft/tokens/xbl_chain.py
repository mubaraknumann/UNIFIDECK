"""XBL/XSTS token chain mixin — builds and caches Xbox Live and GSSV tokens derived from the OAuth access token."""

from __future__ import annotations
import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from ..microsoft_auth import build_xbl_chain, request_xsts_token
if TYPE_CHECKING:
    from ..microsoft_config import MicrosoftConfig
logger = logging.getLogger(__name__)
@dataclass
class XBLTokenChain:
    """Cached XBL/XSTS chain ready for an Xbox-API call.

    Attributes:
        xsts_token: XSTS token to put in the ``Authorization`` header.
        user_hash: XBL user hash (``uhs``).
        xuid: Xbox user ID, if surfaced by XSTS.
        xbl_token: Underlying XBL user token (kept so a GSSV
            chain can be derived without redoing the XBL leg).
    """
    xsts_token: str
    user_hash: str
    xuid: str | None = None
    xbl_token: str | None = None
class XBLChainMixin:
    """Mixin: build XBL + GSSV token chains from the cached access token.

    ``build_chain`` returns the standard ``xboxlive.com`` chain.
    ``build_gssv_chain`` returns a chain bound to the GSSV
    relying party — either piggy-backing on an existing XBL
    token or doing a full from-scratch dance.
    """
    _ms_access_token: str | None
    _config: MicrosoftConfig
    _locale_fn: Callable[[], str]
    async def build_chain(self) -> XBLTokenChain | None:
        """Build the default XBL → XSTS chain (relying party ``http://xboxlive.com``).

        Runs the synchronous ``build_xbl_chain`` in a thread
        executor.

        Returns:
            Populated ``XBLTokenChain``, or ``None`` on missing
            access token or any failure in the chain.
        """
        if not self._ms_access_token:
            return None
        access_token = self._ms_access_token
        try:
            result = await (
                asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: build_xbl_chain(
                        access_token,
                        self._locale_fn(),
                        xbl_auth_url=self._config.xbl_auth_url,
                        xsts_url=self._config.xsts_url,
                        xbl_user_agent=(
                            self._config.xbl_user_agent
                        ),
                    ),
                )
            )
        except Exception as e:
            logger.error(
                "[MicrosoftTokens] XBL chain error: %s", e,
            )
            return None
        if not result:
            return None
        return XBLTokenChain(
            xsts_token=result["xsts_token"],
            user_hash=result["user_hash"],
            xuid=result.get("xuid"),
            xbl_token=result.get("xbl_token"),
        )

    async def build_gssv_chain(
        self,
        xbl_token: str | None = None,
    ) -> XBLTokenChain | None:

        """Build an XSTS chain bound to the GSSV relying party.

        Used by the Game Pass subscription probe and the
        xCloud launcher. When ``xbl_token`` is provided we
        skip the XBL leg and re-trade just the XSTS token.

        Args:
            xbl_token: Optional cached XBL token to reuse.

        Returns:
            Populated ``XBLTokenChain``, or ``None`` on failure.
        """
        relying_party = self._config.gssv_relying_party
        if xbl_token:
            return await self._gssv_from_xbl_token(
                xbl_token, relying_party,
            )
        return await self._gssv_from_scratch(relying_party)
    async def _gssv_from_xbl_token(
        self, xbl_token: str, relying_party: str,
    ) -> XBLTokenChain | None:
        """Build a GSSV chain by re-trading an existing XBL token.

        Args:
            xbl_token: Cached XBL user token.
            relying_party: GSSV relying-party URL.

        Returns:
            Populated ``XBLTokenChain``, or ``None`` on XSTS
            failure / XErr response / missing user hash.
        """
        loop = asyncio.get_event_loop()
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: request_xsts_token(
                    xbl_token=xbl_token,
                    xsts_rp=relying_party,
                    locale=self._locale_fn(),
                    xsts_url=self._config.xsts_url,
                    xbl_user_agent=self._config.xbl_user_agent,
                ),
            )
        except Exception as e:
            logger.error(
                "[MicrosoftTokens] GSSV XSTS error: %s", e,
            )
            return None
        if not resp or "XErr" in resp:
            return None
        xsts_token = resp.get("Token")
        if not xsts_token:
            return None
        claims = resp.get("DisplayClaims", {}).get("xui", [{}])
        user_hash = claims[0].get("uhs") if claims else None
        if not user_hash:
            return None
        return XBLTokenChain(
            xsts_token=xsts_token,
            user_hash=user_hash,
            xuid=claims[0].get("xid") if claims else None,
            xbl_token=xbl_token,
        )
    async def _gssv_from_scratch(
        self, relying_party: str,
    ) -> XBLTokenChain | None:
        """Build a full GSSV chain from the access token, no XBL token reuse.

        Args:
            relying_party: GSSV relying-party URL.

        Returns:
            Populated ``XBLTokenChain``, or ``None`` on failure.
        """
        if not self._ms_access_token:
            return None
        access_token = self._ms_access_token
        try:
            result = await (
                asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: build_xbl_chain(
                        access_token,
                        self._locale_fn(),
                        xbl_auth_url=self._config.xbl_auth_url,
                        xsts_url=self._config.xsts_url,
                        xbl_user_agent=(
                            self._config.xbl_user_agent
                        ),
                        xsts_relying_party=relying_party,
                    ),
                )
            )
        except Exception as e:
            logger.error(
                "[MicrosoftTokens] GSSV chain error: %s", e,
            )
            return None
        if not result:
            return None
        return XBLTokenChain(
            xsts_token=result["xsts_token"],
            user_hash=result["user_hash"],
            xuid=result.get("xuid"),
            xbl_token=result.get("xbl_token"),
        )