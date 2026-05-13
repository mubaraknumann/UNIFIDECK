"""OAuth redirect-capture via CDP target polling.

OP-15a | py_modules/unifideck/auth/browser.py

Polls the CDP target list looking for a tab whose URL
matches one of the registered OAuth callback URIs.
When a match is found, extracts the query +
fragment params (handles both the standard
``?code=...`` flow and the implicit fragment flow).

Also provides:

* ``close_oauth_tab`` — close the captured tab after
  extraction;
* ``clear_store_cookies`` — wipe cookies for a given
  domain (used to force fresh login).

``CDPOAuthMonitor`` is kept as a backward-compat alias
for the renamed ``OAuthBrowserMonitor``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from ..utils.config_helpers import get_cfg

if TYPE_CHECKING:
    from ..cdp.cdp_client import CDPClient
    from ..config import ConfigManager

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 0.5
DEFAULT_OAUTH_TIMEOUT = 300


@dataclass
class AuthCaptureResult:
    """Typed result of a redirect-capture wait.

    Attributes:
        success: True iff a matching redirect was
            seen before timeout.
        redirect_url: full matched URL (None on
            timeout).
        params: extracted query + fragment params
            flattened to ``{k: v}``.
        elapsed_seconds: wall-clock time spent
            waiting.
        error: free-form error code (``"timeout"``
            on the typical failure).
    """

    success: bool
    redirect_url: str | None = None
    params: dict[str, str] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    error: str | None = None

    @property
    def code(self) -> str | None:
        """Convenience accessor for the standard ``code`` OAuth param.

        Returns:
            The OAuth authorization code, or ``None``
            when absent.
        """
        return self.params.get("code")

    @property
    def state(self) -> str | None:
        """Convenience accessor for the standard ``state`` OAuth param.

        Returns:
            The OAuth state token, or ``None`` when
            absent.
        """
        return self.params.get("state")

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict for RPC payloads.

        Returns:
            Five-key dict.
        """
        return {
            "success": self.success,
            "redirect_url": self.redirect_url,
            "params": dict(self.params),
            "elapsed_seconds": self.elapsed_seconds,
            "error": self.error,
        }


def extract_oauth_params(url: str) -> dict[str, str]:
    """Extract OAuth params from a URL's query string and fragment.

    Two passes:

    1. ``parse_qs`` on the query string;
    2. ``parse_qs`` on the fragment (implicit flow);
       fragment entries don't overwrite query
       entries with the same name.

    For each param, only the first value is kept
    (OAuth params are single-valued).

    Args:
        url: the redirect URL.

    Returns:
        Flat dict of param name → first value.
    """
    if not url:
        return {}
    parsed = urlparse(url)
    out: dict[str, str] = {}
    for key, values in parse_qs(parsed.query).items():
        if values:
            out[key] = values[0]
    if parsed.fragment:
        for key, values in parse_qs(parsed.fragment).items():
            if values and key not in out:
                out[key] = values[0]
    return out


def match_redirect(
    url: str,
    allowed_uris: Iterable[str],
) -> bool:
    """Test whether ``url`` matches one of ``allowed_uris`` (prefix match).

    Scheme + host validation rules:

    * ``https://...`` always allowed;
    * ``http://localhost`` (or ``127.0.0.1`` or
      ``[::1]``) allowed — for desktop apps using
      local-loopback callbacks;
    * Anything else rejected (prevents phishing
      redirects).

    The match itself is a startswith on the
    ``scheme://netloc/path`` prefix (ignoring query +
    fragment, since those carry the OAuth payload).

    Args:
        url: candidate URL.
        allowed_uris: iterable of allowed prefix
            URLs.

    Returns:
        True on match.
    """
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http"
        and parsed.hostname
        in (
            "localhost",
            "127.0.0.1",
            "[::1]",
        )
    ):
        return False
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    for prefix in allowed_uris:
        if not prefix:
            continue
        prefix_parsed = urlparse(prefix)
        prefix_base = (
            f"{prefix_parsed.scheme}://{prefix_parsed.netloc}{prefix_parsed.path}"
        )
        if base.startswith(prefix_base):
            return True
    return False


def _cfg(config: ConfigManager | None, key: str, default: Any) -> Any:
    """Thin wrapper around ``get_cfg`` — local convention.

    Args:
        config: optional ``ConfigManager``.
        key: dotted key.
        default: fallback.

    Returns:
        Config value or default.
    """
    return get_cfg(config, key, default)


class OAuthBrowserMonitor:
    """CDP-based OAuth redirect monitor."""

    def __init__(
        self,
        cdp_client: CDPClient,
        config: ConfigManager | None = None,
    ) -> None:
        """Bind the CDP client + resolve poll/timeout overrides.

        Two config keys read at construction:

        * ``auth.browser_poll_interval_seconds``
          (default 0.5) — how often to poll CDP
          targets;
        * ``auth.browser_oauth_timeout_seconds``
          (default 300) — overall OAuth timeout.

        Args:
            cdp_client: live ``CDPClient``.
            config: optional ``ConfigManager``.
        """
        self._cdp = cdp_client
        self._config = config
        self._poll_interval = float(
            get_cfg(
                config,
                "auth.browser_poll_interval_seconds",
                DEFAULT_POLL_INTERVAL,
            )
        )
        self._default_timeout = float(
            get_cfg(
                config,
                "auth.browser_oauth_timeout_seconds",
                DEFAULT_OAUTH_TIMEOUT,
            )
        )

    async def wait_for_redirect(
        self,
        allowed_uris: list[str],
        timeout: float | None = None,
    ) -> AuthCaptureResult:
        """Poll CDP targets until one matches an allowed redirect or timeout.

        Loop:

        * List CDP targets (errors → DEBUG log + sleep);
        * For each target, test ``match_redirect``;
        * On match: extract params + return success;
        * Otherwise sleep ``poll_interval`` and
          retry.

        On timeout returns a result with
        ``error="timeout"`` and the elapsed time
        (useful for diagnostics — slow OAuth providers
        sometimes time out the user, not us).

        Args:
            allowed_uris: list of allowed redirect URI
                prefixes (must include scheme).
            timeout: override the default timeout;
                ``None`` uses the constructor default.

        Returns:
            ``AuthCaptureResult``.
        """
        deadline = time.monotonic() + (
            timeout if timeout is not None else self._default_timeout
        )
        start = time.monotonic()
        while time.monotonic() < deadline:
            try:
                targets = await self._list_targets()
            except Exception as e:
                logger.debug(
                    "[auth/browser] target list: %s",
                    e,
                )
                await asyncio.sleep(self._poll_interval)
                continue
            for target in targets:
                url = target.get("url", "")
                if match_redirect(url, allowed_uris):
                    elapsed = time.monotonic() - start
                    params = extract_oauth_params(url)
                    logger.info(
                        "[auth/browser] captured redirect after %.1fs",
                        elapsed,
                    )
                    return AuthCaptureResult(
                        success=True,
                        redirect_url=url,
                        params=params,
                        elapsed_seconds=elapsed,
                    )
            await asyncio.sleep(self._poll_interval)
        return AuthCaptureResult(
            success=False,
            error="timeout",
            elapsed_seconds=time.monotonic() - start,
        )

    async def close_oauth_tab(
        self,
        url_substring: str,
    ) -> bool:
        """Find a CDP target whose URL contains ``url_substring`` and close it.

        Used after the auth flow to clean up the
        leftover browser tab. Returns False on miss
        (no matching tab) or any CDP error (logged at
        DEBUG).

        Args:
            url_substring: distinguishing fragment of
                the tab's URL.

        Returns:
            True if a tab was found and closed.
        """
        try:
            targets = await self._list_targets()
        except Exception:
            return False
        for target in targets:
            if url_substring in target.get("url", ""):
                target_id = target.get("id")
                if not target_id:
                    continue
                try:
                    await self._cdp.close_target(target_id)
                    return True
                except Exception as e:
                    logger.debug(
                        "[auth/browser] close failed: %s",
                        e,
                    )
                    return False
        return False

    async def clear_store_cookies(self, domain: str) -> bool:
        """Evict every cookie for ``domain`` via JS document.cookie expiry.

        Validates the domain against a strict regex
        before injecting it into JS (defence-in-depth
        against JS injection). On a non-conforming
        domain, rejects with a WARN log + False
        return.

        The eviction technique: enumerate every
        ``document.cookie`` entry and overwrite each
        with an expired timestamp + the target
        domain. Browsers respect this and drop the
        cookies.

        Args:
            domain: cookie domain (e.g.
                ``"epicgames.com"``).

        Returns:
            True if the JS eval succeeded.
        """
        if not re.match(
            r"^[a-zA-Z0-9][a-zA-Z0-9.\-]*$",
            domain,
        ):
            logger.warning(
                "[auth/browser] rejected invalid cookie domain: %r",
                domain,
            )
            return False
        try:
            await self._cdp.eval_js(
                "document.cookie.split(';').forEach(c => "
                "document.cookie = c.replace(/^ +/, '')"
                ".replace(/=.*/,"
                f" '=;expires=Thu, 01 Jan 1970 00:00:00 GMT;"
                f"path=/;domain={domain}'));",
            )
            return True
        except Exception as e:
            logger.debug(
                "[auth/browser] cookie clear failed: %s",
                e,
            )
            return False

    async def _list_targets(self) -> list[dict[str, Any]]:
        """Forward to ``CDPClient.list_targets``, returning ``[]`` on error.

        Wraps the throw → empty list conversion so the
        polling loop in ``wait_for_redirect`` has
        simpler error handling.

        Returns:
            List of CDP target dicts (or empty).
        """
        try:
            return await self._cdp.list_targets()
        except Exception as e:
            logger.debug(
                "[auth/browser] list_targets failed: %s",
                e,
            )
            return []


CDPOAuthMonitor = OAuthBrowserMonitor
