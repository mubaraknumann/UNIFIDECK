"""Low-level CDP primitives — websocket I/O, target discovery, Runtime.evaluate wrappers."""

from __future__ import annotations
import asyncio
import contextlib
import json
import logging
from typing import Any, cast
import aiohttp
from unifideck.cdp.page_inject import list_page_targets
logger = logging.getLogger(__name__)
async def wait_for_titled_target(
    cdp_port: int,
    title_substring: str,
    *,
    timeout: float = 15.0,
    poll_delay: float = 0.25,
) -> dict[str, Any] | None:
    """Poll the CDP target list until one with a matching title appears.

    Args:
        cdp_port: TCP port the browser is listening on.
        title_substring: Case-sensitive substring to match against
            each target's ``title``.
        timeout: Max seconds to wait.
        poll_delay: Sleep between probes.

    Returns:
        The first matching target dict, or ``None`` on timeout.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            targets = await list_page_targets(cdp_port, timeout=3.0)
            for target in targets:
                if title_substring in str(target.get("title", "")):
                    return dict(target)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[cdp] waiting for target failed: %s", exc)
        await asyncio.sleep(poll_delay)
    return None
async def close_target(cdp_port: int, target_id: str) -> None:
    """Issue a CDP close for one target ID via the HTTP endpoint.

    Args:
        cdp_port: TCP port the browser is listening on.
        target_id: Target ID to close.
    """
    close_url = f"http://127.0.0.1:{cdp_port}/json/close/{target_id}"
    async with aiohttp.ClientSession() as session:
        with contextlib.suppress(Exception):
            async with session.get(
                close_url,
                timeout=aiohttp.ClientTimeout(total=3.0),
            ) as response:
                await response.read()
async def close_titled_targets(
    cdp_port: int, title_substring: str,
) -> None:
    """Close every CDP target whose title matches the given substring.

    Best-effort: any exception is swallowed.

    Args:
        cdp_port: TCP port the browser is listening on.
        title_substring: Substring to match against each target's title.
    """
    with contextlib.suppress(Exception):
        targets = await list_page_targets(cdp_port, timeout=3.0)
        for target in targets:
            if title_substring in str(target.get("title", "")):
                await close_target(cdp_port, str(target["id"]))

async def cdp_command(
    websocket: aiohttp.ClientWebSocketResponse,
    msg_id: int,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:

    """Send one CDP method call and wait for the matching reply.

    Filters incoming messages on ``id`` until the reply with the
    expected ``msg_id`` arrives. Closed/error websocket frames
    raise. CDP errors are re-raised as ``RuntimeError``.

    Args:
        websocket: Connected CDP websocket.
        msg_id: Caller-chosen message ID.
        method: CDP method name (e.g. ``"Runtime.evaluate"``).
        params: Optional method parameters.

    Returns:
        The full CDP response dict.

    Raises:
        RuntimeError: Websocket closed/errored, or the CDP
            method returned an error payload.
    """
    await websocket.send_json(
        {
            "id": msg_id,
            "method": method,
            "params": params or {},
        },
    )
    while True:
        message = await websocket.receive(timeout=15)
        if message.type != aiohttp.WSMsgType.TEXT:
            if message.type in (
                aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING,
            ):
                raise RuntimeError("CDP websocket closed")
            if message.type == aiohttp.WSMsgType.ERROR:
                raise RuntimeError("CDP websocket error")
            continue
        payload = json.loads(message.data)
        if payload.get("id") != msg_id:
            continue
        if "error" in payload:
            raise RuntimeError(f"{method} failed: {payload['error']}")
        return cast("dict[str, Any]", payload)
async def evaluate_in_target(
    target: dict[str, Any],
    expression: str,
    *,
    return_by_value: bool = True,
) -> dict[str, Any]:
    """Open a fresh websocket to one target and run ``Runtime.evaluate``.

    Args:
        target: Target dict (must include ``webSocketDebuggerUrl``).
        expression: JavaScript expression.
        return_by_value: Forwarded to CDP — serialize result
            as a value vs return a remote object handle.

    Returns:
        The full CDP response dict.
    """
    async with aiohttp.ClientSession() as session, session.ws_connect(
        target["webSocketDebuggerUrl"],
        heartbeat=10,
        autoping=True,
    ) as websocket:
        return await cdp_command(
            websocket,
            9001,
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": return_by_value,
                "userGesture": True,
            },
        )