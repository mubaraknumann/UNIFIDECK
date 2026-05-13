"""Minimal sync CDP client tuned for the Edge auth/xCloud flows.

OP-15c6 | py_modules/unifideck/auth/edge_browser/cdp_client.py

Sync I/O on top of ``urllib`` rather than aiohttp —
keeps the dependency surface minimal and works inside
``asyncio.to_thread`` calls. The class is built around
the few CDP endpoints we actually use:

* ``/json/version`` — health check + browser-level WS;
* ``/json/list`` — enumerate page targets;
* ``/json/close/<id>`` — close a specific target;
* WebSocket ``Page.navigate`` — drive the page to a
  URL with load-event tracking.

For the websocket path, we drop down to the
``websockets`` library directly since this client
needs only a single short-lived connection per
navigation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class EdgeCDPClient:
    """Tiny CDP client for one Edge instance at a known port."""

    def __init__(self, cdp_port: int) -> None:
        """Capture the port number; nothing else to set up.

        Args:
            cdp_port: ``--remote-debugging-port``
                value Edge was launched with.
        """
        self.cdp_port = cdp_port

    def get_browser_ws_url(self) -> str | None:
        """Hit ``/json/version`` and return the browser-level WS URL.

        Used by ``close_all_targets`` to detect when
        all pages have actually closed (Edge's
        ``/json/version`` stops responding once the
        browser exits).

        Returns ``None`` on any failure — short
        timeout (1 s) is intentional, the probe is
        in a tight loop.

        Returns:
            WebSocket URL string, or ``None``.
        """
        import urllib.request as _req

        try:
            with _req.urlopen(
                f"http://127.0.0.1:{self.cdp_port}/json/version",
                timeout=1,
            ) as r:
                data = json.loads(r.read().decode())
                ws_url = data.get("webSocketDebuggerUrl")
                return ws_url if ws_url else None
        except Exception:
            return None

    def probe_cdp(self) -> bool:
        """Boolean health check on the CDP HTTP endpoint.

        Used during launch to detect when Edge has
        finished starting up. Returns True the moment
        ``/json/version`` responds; doesn't care about
        the actual contents.

        Returns:
            True if responsive.
        """
        import urllib.request

        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.cdp_port}/json/version",
                timeout=1,
            ):
                return True
        except Exception:
            return False

    def list_targets(self) -> list[dict[str, Any]]:
        """Hit ``/json/list`` and return parsed list of CDP target dicts.

        Empty list on any failure or non-list
        response (defensive).

        Returns:
            List of CDP target dicts (possibly empty).
        """
        import urllib.request as _req

        try:
            with _req.urlopen(
                f"http://127.0.0.1:{self.cdp_port}/json/list",
                timeout=1,
            ) as r:
                data = json.loads(r.read().decode())
                return data if isinstance(data, list) else []
        except Exception:
            return []

    async def navigate_tab(
        self,
        url: str,
        timeout: float = 15.0,
    ) -> bool:
        """Drive the first page target to ``url`` via ``Page.navigate``.

        Four-step:

        1. ``list_targets`` to find the first
           type=``"page"`` target;
        2. Open a short-lived websocket to its
           ``webSocketDebuggerUrl``;
        3. Send ``Page.enable`` (subscribes us to
           page lifecycle events) + ``Page.navigate``;
        4. ``_await_navigation_result`` waits for the
           navigation ack + load event with the
           supplied timeout.

        Returns False with a WARN log on:

        * No page target;
        * No WS URL on the target (rare);
        * ``websockets`` library not installed
          (defensive);
        * Any exception during the WS dance.

        Args:
            url: URL to navigate to.
            timeout: overall wait for the load event.

        Returns:
            True on successful navigation.
        """
        targets = self.list_targets()
        page_target = next(
            (t for t in targets if t.get("type") == "page"),
            None,
        )
        if not page_target:
            logger.warning("[Edge] navigate_tab: no page target found")
            return False
        ws_url = page_target.get("webSocketDebuggerUrl")
        if not ws_url:
            logger.warning(
                "[Edge] navigate_tab: no webSocketDebuggerUrl",
            )
            return False
        try:
            import websockets
        except ImportError:
            logger.warning(
                "[Edge] navigate_tab: websockets not available",
            )
            return False
        try:
            async with websockets.connect(ws_url, close_timeout=3) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "id": 1,
                            "method": "Page.enable",
                            "params": {},
                        }
                    )
                )
                try:
                    await asyncio.wait_for(ws.recv(), timeout=3)
                except TimeoutError:
                    pass
                await ws.send(
                    json.dumps(
                        {
                            "id": 2,
                            "method": "Page.navigate",
                            "params": {"url": url},
                        }
                    )
                )
                deadline = asyncio.get_event_loop().time() + timeout
                return await _await_navigation_result(
                    ws,
                    deadline,
                    url,
                )
        except Exception as exc:
            logger.warning("[Edge] navigate_tab failed: %s", exc)
            return False

    async def close_all_targets(self, *, log_prefix: str) -> bool:
        """Send ``/json/close/<id>`` for every target and wait for browser shutdown.

        Used during a graceful Edge shutdown — closing
        every CDP target causes Edge to exit cleanly
        (last-window-closed semantics).

        Per-target:

        * Skip empty ids;
        * Run the close HTTP call in a thread (urllib
          is sync);
        * 404 errors are silenced (target already
          gone — race condition between list and
          close);
        * Other HTTP/exception → WARN log + continue.

        After closes, polls ``get_browser_ws_url`` up
        to 5 s; once it stops responding, the browser
        has fully exited.

        Args:
            log_prefix: ``"auth"`` / ``"xCloud"`` for
                log context.

        Returns:
            True if any target was closed (vs nothing
            to do).
        """
        targets = self.list_targets()
        if not targets:
            return False
        import urllib.error as _err
        import urllib.request as _req

        def _close_target(target_id: str) -> None:
            """Sync HTTP GET for one ``/json/close/<id>`` endpoint.

            Args:
                target_id: CDP target id.
            """
            with _req.urlopen(
                f"http://127.0.0.1:{self.cdp_port}/json/close/{target_id}",
                timeout=2,
            ) as r:
                r.read()

        closed_any = False
        for target in targets:
            target_id = target.get("id")
            if not target_id:
                continue
            try:
                await asyncio.to_thread(_close_target, target_id)
                closed_any = True
            except _err.HTTPError as e:
                if e.code != 404:
                    logger.warning(
                        "[Edge] Could not close %s target %s: %s",
                        log_prefix,
                        target_id,
                        e,
                    )
            except Exception as e:
                logger.warning(
                    "[Edge] Could not close %s target %s: %s",
                    log_prefix,
                    target_id,
                    e,
                )
        if closed_any:
            for _ in range(20):
                await asyncio.sleep(0.25)
                if not self.get_browser_ws_url():
                    break
            logger.info(
                "[Edge] Closed %s browser targets via DevTools HTTP",
                log_prefix,
            )
        return closed_any


async def _await_navigation_result(
    ws: Any,
    deadline: float,
    url: str,
) -> bool:
    """Pump the WebSocket waiting for navigate ack + load event.

    State machine:

    * ``id=2`` reply → either ``"error"`` (return
      False) or success (set ``got_navigate_ok``,
      keep listening for load).
    * ``method=Page.frameStoppedLoading`` or
      ``Page.loadEventFired`` with
      ``got_navigate_ok=True`` → success.

    Timeout (loop exhausted) with
    ``got_navigate_ok=True`` → log a "load timed
    out" line + return True. The navigation itself
    succeeded; the page just took longer than the
    timeout. Caller decides whether that's OK.

    Args:
        ws: open ``websockets`` connection.
        deadline: monotonic deadline.
        url: target URL (for logging).

    Returns:
        True on navigation success (with or without
        load event).
    """
    got_navigate_ok = False
    while asyncio.get_event_loop().time() < deadline:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(
                ws.recv(),
                timeout=remaining,
            )
        except TimeoutError:
            break
        msg = json.loads(raw)
        if msg.get("id") == 2:
            if "error" in msg:
                logger.warning(
                    "[Edge] navigate_tab error: %s",
                    msg["error"],
                )
                return False
            got_navigate_ok = True
        if (
            msg.get("method")
            in (
                "Page.frameStoppedLoading",
                "Page.loadEventFired",
            )
            and got_navigate_ok
        ):
            logger.info(
                "[Edge] navigate_tab: loaded %s",
                url,
            )
            return True
    if got_navigate_ok:
        logger.info(
            "[Edge] navigate_tab: navigation sent, load timed out for %s",
            url,
        )
        return True
    return False
