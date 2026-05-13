"""Async Chrome DevTools Protocol client over WebSocket.

OP-13b | py_modules/unifideck/cdp/cdp_client.py

Minimal CDP client that:

* Lists CDP targets via the ``/json`` HTTP endpoint;
* Picks a target by URL substring;
* Opens a WebSocket to it;
* Sends id'd command messages with a response-future
  map for correlation;
* Spawns a background receive loop that resolves
  pending futures.

Used by ``SteamCSSInjector`` (Steam frontend) and the
xCloud auth flow (Edge instance with remote debugging
flag). Both share the same protocol — only the target
selection differs.

Configuration via ``cdp.host``, ``cdp.port``,
``cdp.eval_timeout_seconds``,
``cdp.response_timeout_seconds`` — all overridable per
deployment.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, cast

from unifideck.utils.config_helpers import get_cfg

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..config import ConfigManager


class CDPClient:
    """Minimal async CDP client with request/response correlation."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        config: ConfigManager | None = None,
    ) -> None:
        """Resolve config + initialise empty state.

        Three timeouts:

        * ``eval_timeout`` — used by
          ``wait_for_url`` polling loop;
        * ``response_timeout`` — per-command response
          wait (covers slow tab responses).

        Constructor doesn't open a websocket — use
        ``connect`` for that. State (request counter,
        pending-future map, recv task) starts empty.

        Args:
            host: override CDP host (default
                ``"127.0.0.1"``).
            port: override CDP port (default ``8080``).
            config: optional ``ConfigManager`` for
                config-driven overrides.
        """
        self._host = host or get_cfg(config, "cdp.host", "127.0.0.1")
        self._port = port or int(get_cfg(config, "cdp.port", 8080))
        self._eval_timeout = float(get_cfg(config, "cdp.eval_timeout_seconds", 30))
        self._response_timeout = float(
            get_cfg(config, "cdp.response_timeout_seconds", 10)
        )
        self._ws = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._recv_task: asyncio.Task | None = None

    async def connect(self, target_url_substring: str = "") -> bool:
        """List targets, pick one by URL substring, open the WebSocket.

        Two-step:

        1. ``_list_targets`` to enumerate active CDP
           targets (HTTP GET on ``/json``);
        2. ``_pick_target`` filters to type=``"page"``
           + URL matching ``target_url_substring``.

        Empty substring matches the first page-typed
        target (used during early dev / tests).
        Connect failure (target not found, websocket
        refused) returns False so callers can decide
        retry policy.

        Args:
            target_url_substring: URL filter.

        Returns:
            True on successful connect + recv-loop
            start.
        """
        targets = await self._list_targets()
        target = self._pick_target(targets, target_url_substring)
        if not target or "webSocketDebuggerUrl" not in target:
            return False
        try:
            import websockets

            self._ws = await websockets.connect(
                target["webSocketDebuggerUrl"],
                max_size=None,
            )
        except Exception as e:
            logger.error("[CDPClient] ws connect failed: %s", e)
            return False
        self._recv_task = asyncio.create_task(self._recv_loop())
        return True

    async def disconnect(self) -> None:
        """Cancel the recv loop and close the WebSocket cleanly.

        Two-step: cancel + await on the recv task
        (swallow the ``CancelledError`` from the
        cooperative cancellation); then close the
        websocket. Both steps are guarded against
        ``None`` so it's safe to call before
        ``connect``.
        """
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def inject_css(self, css: str) -> bool:
        """Convenience: build a JS IIFE that creates a ``<style>`` and runs it.

        Lower-level than ``SteamCSSInjector.inject_css``
        — no marker tracking, just one-shot injection
        of arbitrary CSS. Used by callers that don't
        need updateable CSS.

        Args:
            css: CSS source.

        Returns:
            True if eval succeeded and returned truthy.
        """
        expression = (
            "(() => {"
            "const s = document.createElement('style');"
            f"s.textContent = {json.dumps(css)};"
            "document.head.appendChild(s);"
            "return true; })()"
        )
        result = await self.evaluate(expression)
        return bool(result and result.get("result", {}).get("value"))

    async def evaluate(self, expression: str) -> dict | None:
        """Send ``Runtime.evaluate`` for ``expression`` with ``returnByValue=true``.

        Returns the full CDP result object so callers
        can inspect both the value and metadata
        (exceptionDetails, type, etc.).

        Args:
            expression: JS expression to evaluate.

        Returns:
            Full CDP result dict, or ``None`` on
            transport failure.
        """
        return await self._send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
            },
        )

    async def eval_js(self, expression: str) -> Any:
        """Helper — call ``evaluate`` and unwrap ``result.value``.

        Cleans up the most common usage pattern.
        Returns ``None`` on failure or when the
        evaluation produced no value.

        Args:
            expression: JS expression.

        Returns:
            The unwrapped value, or ``None``.
        """
        result = await self.evaluate(expression)
        if not result:
            return None
        return result.get("result", {}).get("value")

    async def list_targets(self) -> list[dict[str, Any]]:
        """Public wrapper around ``_list_targets`` for callers needing introspection.

        Returns:
            List of CDP target dicts.
        """
        return await self._list_targets()

    async def close_target(self, target_id: str) -> bool:
        """Close a CDP target via the ``/json/close/<id>`` endpoint.

        Returns False on empty id (defensive) or any
        HTTP failure. Used by the xCloud flow to clean
        up after auth.

        Args:
            target_id: CDP target id.

        Returns:
            True on 200 OK.
        """
        if not target_id:
            return False
        import aiohttp

        url = f"http://{self._host}:{self._port}/json/close/{target_id}"
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(url, timeout=5) as resp,
            ):
                return cast("bool", resp.status == 200)
        except Exception as e:
            logger.debug(
                "[CDPClient] close_target failed: %s",
                e,
            )
            return False

    async def navigate(self, url: str) -> bool:
        """Send ``Page.navigate`` to make the current target load ``url``.

        Args:
            url: target URL.

        Returns:
            True if the command was acknowledged.
        """
        result = await self._send("Page.navigate", {"url": url})
        return result is not None

    async def wait_for_url(
        self, substring: str, timeout: float | None = None
    ) -> str | None:
        """Poll ``window.location.href`` until it contains ``substring`` or times out.

        500 ms poll interval. Used by the xCloud flow
        to wait for redirects (the auth pages redirect
        through several URLs before landing on the
        token-carrying one).

        Args:
            substring: URL fragment to wait for.
            timeout: maximum wait in seconds (defaults
                to ``eval_timeout``).

        Returns:
            The matched URL on success, ``None`` on
            timeout.
        """
        deadline = asyncio.get_event_loop().time() + (
            timeout if timeout is not None else self._eval_timeout
        )
        while asyncio.get_event_loop().time() < deadline:
            result = await self.evaluate(
                "window.location.href",
            )
            if result:
                url = result.get("result", {}).get("value") or ""
                if substring in url:
                    return url
            await asyncio.sleep(0.5)
        return None

    async def _list_targets(self) -> list[dict[str, Any]]:
        """HTTP GET ``/json`` and return the parsed target list.

        Returns ``[]`` on non-200 or any network error
        — caller treats empty as "no targets".

        Returns:
            List of CDP target dicts, or ``[]``.
        """
        import aiohttp

        url = f"http://{self._host}:{self._port}/json"
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(url, timeout=5) as resp,
            ):
                if resp.status != 200:
                    return []
                return cast("list[dict[str, Any]]", await resp.json())
        except Exception as e:
            logger.debug(
                "[CDPClient] list targets failed: %s",
                e,
            )
            return []

    @staticmethod
    def _pick_target(
        targets: list[dict[str, Any]], substring: str
    ) -> dict[str, Any] | None:
        """Return the first ``type=="page"`` target whose URL contains ``substring``.

        Empty substring matches any page target. Skips
        non-page targets (background pages, iframes,
        service workers) which can't be CDP-connected
        for the purposes we care about.

        Args:
            targets: list of CDP target dicts.
            substring: URL filter.

        Returns:
            Matching target or ``None``.
        """
        for t in targets:
            if t.get("type") != "page":
                continue
            if not substring or substring in t.get("url", ""):
                return t
        return None

    async def _send(self, method: str, params: dict[str, Any]) -> dict | None:
        """Send one id'd CDP message and await the response on a future.

        Five-step:

        1. Bump the request counter;
        2. Create a future, register it in
           ``_pending[id]``;
        3. Send the JSON message;
        4. ``wait_for`` the future with
           ``response_timeout``;
        5. Pop the pending entry in ``finally``.

        Returns ``None`` on no-websocket (called
        before connect) or timeout. The recv loop
        is responsible for resolving the future.

        Args:
            method: CDP method name.
            params: method params dict.

        Returns:
            Response ``result`` dict, or ``None`` on
            timeout / no-ws.
        """
        if not self._ws:
            return None
        self._request_id += 1
        req_id = self._request_id
        message = {
            "id": req_id,
            "method": method,
            "params": params,
        }
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        try:
            await self._ws.send(json.dumps(message))
            return await asyncio.wait_for(
                future,
                timeout=self._response_timeout,
            )
        except TimeoutError:
            logger.warning(
                "[CDPClient] timeout on %s",
                method,
            )
            return None
        finally:
            self._pending.pop(req_id, None)

    async def _recv_loop(self) -> None:
        """Background coroutine — pump the WebSocket, resolve pending futures.

        Loops indefinitely until the websocket is
        closed or the task is cancelled. For each
        received message:

        * Decode JSON (silently skip on decode error);
        * If it has an ``id`` matching a pending
          request, resolve that future with the
          message's ``result``.

        Cancellation is re-raised (cooperative); other
        exceptions log at WARN and the loop exits
        (the next ``_send`` will fail with ``None``).
        """
        if self._ws is None:
            logger.warning(
                "[CDPClient] _recv_loop started before connect()",
            )
            return
        ws = self._ws
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                req_id = msg.get("id")
                if req_id and req_id in self._pending:
                    self._pending[req_id].set_result(msg.get("result"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[CDPClient] recv loop error: %s", e)
