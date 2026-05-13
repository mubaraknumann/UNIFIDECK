"""Shared HTTP helpers for the GOG store — SSL context + JSON GET.

OP-22-gog-http | py_modules/unifideck/stores/gog/http.py

Centralises three concerns used across multiple
GOG modules (library, tokens, updates, dlc):

* ``build_ssl_context`` — thin wrapper around
  ``core.net.ssl_ctx_strict`` so all GOG HTTP
  goes through the same strict context (cert
  validation, pinned cipher suites);
* ``fetch_json_get`` — async-safe wrapper around
  ``urllib.request`` returning parsed JSON or
  ``None`` on any failure.

The async wrapper uses ``asyncio.to_thread`` (not
``run_in_executor``) so it doesn't depend on the
event loop being the default loop; this matters
because GOG library refreshes can be triggered
from a worker thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import urllib.request
from collections.abc import Mapping
from typing import Any

from ...core.net import ssl_ctx_strict

_logger = logging.getLogger(__name__)


def build_ssl_context() -> ssl.SSLContext:
    """Return the project-standard strict SSL context for GOG endpoints.

    Thin re-export of ``core.net.ssl_ctx_strict``
    — every GOG HTTP call should go through this
    so any future changes to TLS pinning land in
    one place.

    Returns:
        Strict ``ssl.SSLContext`` instance.
    """
    return ssl_ctx_strict()


async def fetch_json_get(
    url: str,
    *,
    bearer: str | None = None,
    user_agent: str,
    timeout: float = 15.0,
    extra_headers: Mapping[str, str] | None = None,
    log_prefix: str = "[GOGHttp]",
) -> Any | None:
    """Async GET that parses JSON and returns ``None`` on any failure.

    Pipeline:

    1. Build headers — UA always set, optional
       ``Authorization: Bearer …``, plus
       caller-provided ``extra_headers`` (merged
       last so callers can override defaults);
    2. Run blocking ``urllib`` GET in a worker
       thread via ``asyncio.to_thread``;
    3. Non-200 → log + return ``None`` (the
       caller decides whether to retry);
    4. Any exception (network, timeout, JSON
       parse) → log + return ``None``.

    The ``log_prefix`` lets each call site
    self-identify in logs (e.g. ``"[GOGLib]"``,
    ``"[GOGTokens]"``) without rewriting the
    helper.

    Args:
        url: target URL.
        bearer: optional OAuth bearer.
        user_agent: required UA string.
        timeout: per-request timeout in seconds.
        extra_headers: extra headers (overrides
            defaults).
        log_prefix: tag for warnings.

    Returns:
        Parsed JSON on 200, ``None`` otherwise.
    """
    headers: dict[str, str] = {"User-Agent": user_agent}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if extra_headers:
        headers.update(extra_headers)

    def _sync() -> Any | None:
        """Blocking GET + JSON parse — runs in a worker thread.

        Returns:
            Parsed JSON, or ``None`` on any
            error (non-200 or exception).
        """
        try:
            ctx = build_ssl_context()
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(
                req,
                timeout=timeout,
                context=ctx,
            ) as response:
                if response.status != 200:
                    _logger.warning(
                        "%s GET %s → HTTP %d",
                        log_prefix,
                        url,
                        response.status,
                    )
                    return None
                return json.loads(response.read().decode())
        except Exception as e:
            _logger.warning(
                "%s GET %s failed: %s",
                log_prefix,
                url,
                e,
            )
            return None

    return await asyncio.to_thread(_sync)
