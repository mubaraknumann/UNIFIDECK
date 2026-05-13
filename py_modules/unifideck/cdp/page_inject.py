"""Generic CDP page-injection — wait for matching target, eval scripts.

OP-13c | py_modules/unifideck/cdp/page_inject.py

Higher-level than ``cdp_client``: instead of requiring
the caller to construct + connect + send manually, this
module polls the CDP target list, waits for a page
whose URL matches one of the supplied patterns, and runs
a list of JS source strings inside it via
``Runtime.evaluate``.

Used by the xCloud auth flow + Microsoft Edge token
capture, where the plugin doesn't control when the
target page appears.

Implementation notes:

* Two-deep WebSocket helpers
  (``_inject_into_target`` + ``_drain_until_reply``)
  because aiohttp's WS API needs explicit message-type
  handling for clean shutdown.
* The top-level ``inject_scripts`` polls until either
  every matching target accepts every script, or the
  deadline expires.
* "Inject once" semantics: as long as at least one
  injection succeeded, the function returns True at
  deadline.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


async def list_page_targets(
    port: int,
    *,
    timeout: float = 3.0,
) -> list[dict[str, Any]]:
    """GET ``http://127.0.0.1:<port>/json`` and return parsed CDP targets.

    Filters the response to dict entries only — CDP
    occasionally returns trailing primitives that
    confuse downstream consumers.

    Raises on HTTP error (``raise_for_status``) — the
    caller catches at the polling layer.

    Args:
        port: CDP HTTP port.
        timeout: total request timeout.

    Returns:
        List of CDP target dicts.
    """
    url = f"http://127.0.0.1:{port}/json"
    async with (
        aiohttp.ClientSession() as session,
        session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response,
    ):
        response.raise_for_status()
        payload = await response.json(content_type=None)
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _target_url_matches(
    target: dict[str, Any],
    patterns: list[str],
) -> bool:
    """Return whether ``target``'s URL contains any of the URL patterns.

    Empty patterns are skipped (defensive — caller
    might pass an empty string). The check is plain
    substring, not regex — patterns are stable URL
    fragments.

    Args:
        target: CDP target dict.
        patterns: list of URL substrings.

    Returns:
        True on any match.
    """
    url = str(target.get("url", ""))
    return any(pattern and pattern in url for pattern in patterns)


_CLOSE_MSG_TYPES = (
    aiohttp.WSMsgType.CLOSED,
    aiohttp.WSMsgType.CLOSING,
    aiohttp.WSMsgType.ERROR,
)


async def _drain_until_reply(
    websocket: aiohttp.ClientWebSocketResponse,
    msg_id: int,
    ws_timeout: float,
    logger_prefix: str,
) -> bool:
    """Drain incoming WS messages until the matching reply id arrives.

    CDP sends asynchronous events (Network, Console,
    etc.) interleaved with command replies, so a
    naive ``await receive()`` may not return our
    reply. This loop:

    * Exits with False on connection close/error;
    * Skips non-TEXT frames;
    * Parses each TEXT frame, skips entries whose
      ``id`` doesn't match ``msg_id``;
    * On matching id, returns True (success) or
      False (error field present).

    Args:
        websocket: open aiohttp WS.
        msg_id: id of the expected reply.
        ws_timeout: per-receive timeout.
        logger_prefix: tag for log lines.

    Returns:
        True on success reply, False otherwise.
    """
    while True:
        message = await websocket.receive(timeout=ws_timeout)
        if message.type in _CLOSE_MSG_TYPES:
            logger.debug(
                "[%s] websocket closed during inject",
                logger_prefix,
            )
            return False
        if message.type != aiohttp.WSMsgType.TEXT:
            continue
        payload = json.loads(message.data)
        if payload.get("id") != msg_id:
            continue
        if "error" in payload:
            logger.debug(
                "[%s] Runtime.evaluate error: %s",
                logger_prefix,
                payload["error"],
            )
            return False
        return True


async def _inject_into_target(
    target: dict[str, Any],
    sources: list[str],
    *,
    ws_timeout: float,
    logger_prefix: str,
) -> bool:
    """Connect a fresh WS to ``target`` and evaluate every script in order.

    Per-script flow:

    * Skip empty entries (defensive);
    * Send a ``Runtime.evaluate`` with
      ``awaitPromise=true`` (handles async IIFEs),
      ``returnByValue=true`` (we don't need ObjectId
      back), ``userGesture=true`` (lets pages perform
      gesture-gated actions).
    * Await the matching reply via
      ``_drain_until_reply``. Failure → return False
      and abort the remaining scripts.

    Catches ``TimeoutError``, ``ClientError``,
    ``OSError`` — the typical transport failure
    classes.

    Args:
        target: CDP target dict (must carry
            ``webSocketDebuggerUrl``).
        sources: list of JS source strings.
        ws_timeout: per-message receive timeout.
        logger_prefix: log tag.

    Returns:
        True if every script ran successfully.
    """
    ws_url = target.get("webSocketDebuggerUrl")
    if not isinstance(ws_url, str) or not ws_url:
        return False
    msg_id = 0
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.ws_connect(
                ws_url,
                heartbeat=10,
                autoping=True,
                timeout=aiohttp.ClientTimeout(total=ws_timeout),
            ) as websocket,
        ):
            for source in sources:
                if not source:
                    continue
                msg_id += 1
                await websocket.send_json(
                    {
                        "id": msg_id,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": source,
                            "awaitPromise": True,
                            "returnByValue": True,
                            "userGesture": True,
                        },
                    }
                )
                if not await _drain_until_reply(
                    websocket,
                    msg_id,
                    ws_timeout,
                    logger_prefix,
                ):
                    return False
        return True
    except (TimeoutError, aiohttp.ClientError, OSError) as exc:
        logger.debug(
            "[%s] inject into %s failed: %s",
            logger_prefix,
            target.get("id"),
            exc,
        )
        return False


async def inject_scripts(
    port: int,
    sources: list[str],
    *,
    url_patterns: list[str],
    timeout: float = 45.0,
    logger_prefix: str = "cdp-inject",
    poll_delay: float = 0.5,
) -> bool:
    """Poll for matching CDP targets and inject ``sources`` until success or timeout.

    Top-level orchestrator:

    1. Empty inputs → return False fast;
    2. Loop until deadline:

       * List targets (errors → DEBUG log + sleep);
       * Filter to ``type=="page"`` matching the
         patterns;
       * If nothing matches yet → sleep and retry;
       * Otherwise inject into every matching target;
       * Success on at least one target sets
         ``injected_once``.

    3. Return True if at least one injection ever
       succeeded — covers the case where the page
       briefly appears then closes before we finish
       a second round.

    Args:
        port: CDP HTTP port.
        sources: JS source strings.
        url_patterns: URL fragments to match.
        timeout: total deadline in seconds.
        logger_prefix: tag for log lines.
        poll_delay: between-poll sleep.

    Returns:
        True if anything was injected before timeout.
    """
    if not sources or not url_patterns:
        return False
    deadline = asyncio.get_running_loop().time() + timeout
    injected_once = False
    while asyncio.get_running_loop().time() < deadline:
        try:
            targets = await list_page_targets(port, timeout=3.0)
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            logger.debug(
                "[%s] list_page_targets failed: %s",
                logger_prefix,
                exc,
            )
            await asyncio.sleep(poll_delay)
            continue
        page_targets = [
            t
            for t in targets
            if t.get("type") == "page" and _target_url_matches(t, url_patterns)
        ]
        if not page_targets:
            await asyncio.sleep(poll_delay)
            continue
        all_ok, had_success = await _inject_into_matching_targets(
            page_targets,
            sources,
            timeout,
            logger_prefix,
        )
        if had_success:
            injected_once = True
        if injected_once and all_ok:
            return True
        await asyncio.sleep(poll_delay)
    if injected_once:
        return True
    logger.warning(
        "[%s] timed out waiting for matching page (patterns=%r)",
        logger_prefix,
        url_patterns,
    )
    return False


async def _inject_into_matching_targets(
    page_targets: list[dict[str, Any]],
    sources: list[str],
    timeout: float,
    logger_prefix: str,
) -> tuple[bool, bool]:
    """Inject into every matching target; return ``(all_ok, had_success)``.

    Two booleans for nuanced control flow at the
    caller:

    * ``all_ok``     — True iff every target accepted
      every script;
    * ``had_success`` — True iff at least one
      injection worked.

    Per-target ws_timeout capped at 15 s regardless of
    overall timeout (keeps the inner WS sessions short
    and lets us cycle quickly on flaky targets).

    Args:
        page_targets: list of matching targets.
        sources: JS sources.
        timeout: overall deadline (clamps ws_timeout).
        logger_prefix: log tag.

    Returns:
        ``(all_ok, had_success)``.
    """
    all_ok = True
    had_success = False
    for target in page_targets:
        ok = await _inject_into_target(
            target,
            sources,
            ws_timeout=min(15.0, timeout),
            logger_prefix=logger_prefix,
        )
        if ok:
            had_success = True
            logger.info(
                "[%s] injected %d script(s) into %s",
                logger_prefix,
                len(sources),
                target.get("url", "?"),
            )
        else:
            all_ok = False
    return all_ok, had_success


@contextlib.asynccontextmanager
async def _session_timeout(total: float):
    """Async-context-manager wrapper around ``aiohttp.ClientTimeout``.

    Currently unused but kept on the API surface for
    callers that want to share a timeout policy across
    multiple requests in one ``async with`` block.

    Args:
        total: total timeout in seconds.

    Yields:
        ``aiohttp.ClientTimeout`` instance.
    """
    yield aiohttp.ClientTimeout(total=total)
