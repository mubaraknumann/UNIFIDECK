"""Deep executable tests — auth/edge_browser/cdp_client.py.

Source : py_modules/unifideck/auth/edge_browser/cdp_client.py
Fiche  : OP (auth/)   Critical — coverage floor 95%.

EdgeCDPClient: urllib probes (/json/version, /json/list,
/json/close) + websocket navigation. urllib + websockets
stubbed; _await_navigation_result driven with a fake ws.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from unifideck.auth.edge_browser.cdp_client import (
    EdgeCDPClient,
    _await_navigation_result,
)


@pytest.fixture()
def cdp() -> EdgeCDPClient:
    return EdgeCDPClient(cdp_port=9222)


def test_module_imports() -> None:
    import unifideck.auth.edge_browser.cdp_client as mod
    assert mod.EdgeCDPClient is EdgeCDPClient


class _Resp:
    def __init__(self, payload: bytes) -> None:
        self._p = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self._p


# ========================================================= #
# get_browser_ws_url / probe_cdp / list_targets
# ========================================================= #
def test_get_browser_ws_url_ok(
    cdp: EdgeCDPClient, monkeypatch,
) -> None:
    import urllib.request as _ur

    monkeypatch.setattr(
        _ur, "urlopen",
        lambda *a, **k: _Resp(json.dumps(
            {"webSocketDebuggerUrl": "ws://x"}
        ).encode()))
    assert cdp.get_browser_ws_url() == "ws://x"


def test_get_browser_ws_url_no_url(
    cdp: EdgeCDPClient, monkeypatch,
) -> None:
    import urllib.request as _ur

    monkeypatch.setattr(
        _ur, "urlopen",
        lambda *a, **k: _Resp(b"{}"))
    assert cdp.get_browser_ws_url() is None


def test_get_browser_ws_url_error(
    cdp: EdgeCDPClient, monkeypatch,
) -> None:
    import urllib.request as _ur

    def _boom(*a, **k):
        raise OSError("conn refused")

    monkeypatch.setattr(_ur, "urlopen", _boom)
    assert cdp.get_browser_ws_url() is None


def test_probe_cdp_true(
    cdp: EdgeCDPClient, monkeypatch,
) -> None:
    import urllib.request as _ur

    monkeypatch.setattr(
        _ur, "urlopen",
        lambda *a, **k: _Resp(b"{}"))
    assert cdp.probe_cdp() is True


def test_probe_cdp_false(
    cdp: EdgeCDPClient, monkeypatch,
) -> None:
    import urllib.request as _ur

    def _boom(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(_ur, "urlopen", _boom)
    assert cdp.probe_cdp() is False


def test_list_targets_ok(
    cdp: EdgeCDPClient, monkeypatch,
) -> None:
    import urllib.request as _ur

    monkeypatch.setattr(
        _ur, "urlopen",
        lambda *a, **k: _Resp(json.dumps(
            [{"type": "page"}]).encode()))
    assert cdp.list_targets() == [{"type": "page"}]


def test_list_targets_not_list(
    cdp: EdgeCDPClient, monkeypatch,
) -> None:
    import urllib.request as _ur

    monkeypatch.setattr(
        _ur, "urlopen",
        lambda *a, **k: _Resp(b'{"not":"list"}'))
    assert cdp.list_targets() == []


def test_list_targets_error(
    cdp: EdgeCDPClient, monkeypatch,
) -> None:
    import urllib.request as _ur

    def _boom(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(_ur, "urlopen", _boom)
    assert cdp.list_targets() == []


# ========================================================= #
# navigate_tab
# ========================================================= #
@pytest.mark.asyncio
async def test_navigate_tab_no_page_target(
    cdp: EdgeCDPClient, monkeypatch,
) -> None:
    monkeypatch.setattr(cdp, "list_targets", lambda: [])
    assert await cdp.navigate_tab("https://x") is False


@pytest.mark.asyncio
async def test_navigate_tab_no_ws_url(
    cdp: EdgeCDPClient, monkeypatch,
) -> None:
    monkeypatch.setattr(
        cdp, "list_targets",
        lambda: [{"type": "page"}])
    assert await cdp.navigate_tab("https://x") is False


@pytest.mark.asyncio
async def test_navigate_tab_no_websockets(
    cdp: EdgeCDPClient, monkeypatch,
) -> None:
    monkeypatch.setattr(
        cdp, "list_targets",
        lambda: [{"type": "page",
                  "webSocketDebuggerUrl": "ws://x"}])
    import sys

    monkeypatch.setitem(sys.modules, "websockets", None)
    assert await cdp.navigate_tab("https://x") is False


@pytest.mark.asyncio
async def test_navigate_tab_success(
    cdp: EdgeCDPClient, monkeypatch,
) -> None:
    monkeypatch.setattr(
        cdp, "list_targets",
        lambda: [{"type": "page",
                  "webSocketDebuggerUrl": "ws://x"}])

    import sys
    import types

    class _WS:
        def __init__(self) -> None:
            self._msgs = [
                json.dumps({"id": 1}),  # Page.enable ack
                json.dumps({"id": 2}),  # navigate ack
                json.dumps({
                    "method": "Page.loadEventFired"}),
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, data: str) -> None:
            return None

        async def recv(self) -> str:
            return self._msgs.pop(0)

    fake = types.ModuleType("websockets")

    def _connect(url: str, **k: Any):
        return _WS()

    fake.connect = _connect  # type: ignore
    monkeypatch.setitem(sys.modules, "websockets", fake)
    out = await cdp.navigate_tab(
        "https://xbox.com", timeout=2)
    assert out is True


# ========================================================= #
# close_all_targets
# ========================================================= #
@pytest.mark.asyncio
async def test_close_all_targets_none(
    cdp: EdgeCDPClient, monkeypatch,
) -> None:
    monkeypatch.setattr(cdp, "list_targets", lambda: [])
    assert await cdp.close_all_targets(
        log_prefix="auth") is False


@pytest.mark.asyncio
async def test_close_all_targets_closes(
    cdp: EdgeCDPClient, monkeypatch,
) -> None:
    monkeypatch.setattr(
        cdp, "list_targets",
        lambda: [{"id": "T1"}, {"id": "T2"},
                 {"no": "id"}])
    import urllib.request as _ur

    monkeypatch.setattr(
        _ur, "urlopen",
        lambda *a, **k: _Resp(b""))
    # ws url None after close -> the wait loop breaks fast
    monkeypatch.setattr(
        cdp, "get_browser_ws_url", lambda: None)
    out = await cdp.close_all_targets(log_prefix="auth")
    assert out is True


@pytest.mark.asyncio
async def test_close_all_targets_http_error(
    cdp: EdgeCDPClient, monkeypatch,
) -> None:
    import urllib.error as _err

    monkeypatch.setattr(
        cdp, "list_targets",
        lambda: [{"id": "T1"}])

    def _boom(*a, **k):
        raise _err.HTTPError(
            "u", 500, "err", {}, None)  # type: ignore

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", _boom)
    out = await cdp.close_all_targets(log_prefix="auth")
    # nothing closed successfully
    assert out is False


@pytest.mark.asyncio
async def test_close_all_targets_404_ignored(
    cdp: EdgeCDPClient, monkeypatch,
) -> None:
    import urllib.error as _err

    monkeypatch.setattr(
        cdp, "list_targets",
        lambda: [{"id": "T1"}])

    def _boom(*a, **k):
        raise _err.HTTPError(
            "u", 404, "gone", {}, None)  # type: ignore

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", _boom)
    out = await cdp.close_all_targets(log_prefix="auth")
    assert out is False


# ========================================================= #
# _await_navigation_result
# ========================================================= #
@pytest.mark.asyncio
async def test_await_nav_result_loaded() -> None:
    class _WS:
        def __init__(self) -> None:
            self._m = [
                json.dumps({"id": 2}),
                json.dumps({
                    "method": "Page.frameStoppedLoading"}),
            ]

        async def recv(self) -> str:
            return self._m.pop(0)

    deadline = asyncio.get_event_loop().time() + 5
    out = await _await_navigation_result(
        _WS(), deadline, "https://x")
    assert out is True


@pytest.mark.asyncio
async def test_await_nav_result_error() -> None:
    class _WS:
        async def recv(self) -> str:
            return json.dumps({
                "id": 2,
                "error": {"message": "nav failed"}})

    deadline = asyncio.get_event_loop().time() + 5
    out = await _await_navigation_result(
        _WS(), deadline, "https://x")
    assert out is False


@pytest.mark.asyncio
async def test_await_nav_result_ack_only_timeout() -> None:
    """Only the navigate ack arrives before the recv times
    out -> still True (cookies set)."""
    class _WS:
        def __init__(self) -> None:
            self._sent = False

        async def recv(self) -> str:
            if not self._sent:
                self._sent = True
                return json.dumps({"id": 2})
            raise TimeoutError

    deadline = asyncio.get_event_loop().time() + 5
    out = await _await_navigation_result(
        _WS(), deadline, "https://x")
    assert out is True


@pytest.mark.asyncio
async def test_await_nav_result_no_ack_deadline() -> None:
    """Deadline reached with no ack -> False."""
    class _WS:
        async def recv(self) -> str:
            raise TimeoutError

    deadline = asyncio.get_event_loop().time() - 1
    out = await _await_navigation_result(
        _WS(), deadline, "https://x")
    assert out is False


# --- remaining branches toward 95% --------------------- #
@pytest.mark.asyncio
async def test_navigate_tab_enable_ack_timeout(
    cdp: EdgeCDPClient, monkeypatch,
) -> None:
    """Page.enable ack times out -> the `except TimeoutError:
    pass` fall-through is taken, navigation still proceeds."""
    monkeypatch.setattr(
        cdp, "list_targets",
        lambda: [{"type": "page",
                  "webSocketDebuggerUrl": "ws://x"}])

    import sys
    import types

    class _WS:
        def __init__(self) -> None:
            self._recv_calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, data: str) -> None:
            return None

        async def recv(self) -> str:
            self._recv_calls += 1
            if self._recv_calls == 1:
                # first recv = Page.enable ack -> time out
                raise TimeoutError
            return json.dumps({"id": 2})

    fake = types.ModuleType("websockets")
    fake.connect = lambda url, **k: _WS()  # type: ignore
    monkeypatch.setitem(sys.modules, "websockets", fake)
    out = await cdp.navigate_tab(
        "https://xbox.com", timeout=2)
    # ack-only -> still True
    assert out is True


@pytest.mark.asyncio
async def test_navigate_tab_ws_connect_raises(
    cdp: EdgeCDPClient, monkeypatch,
) -> None:
    """websockets.connect raising -> outer except -> False."""
    monkeypatch.setattr(
        cdp, "list_targets",
        lambda: [{"type": "page",
                  "webSocketDebuggerUrl": "ws://x"}])

    import sys
    import types

    fake = types.ModuleType("websockets")

    def _connect(url: str, **k: Any):
        raise RuntimeError("ws handshake failed")

    fake.connect = _connect  # type: ignore
    monkeypatch.setitem(sys.modules, "websockets", fake)
    assert await cdp.navigate_tab("https://x") is False


@pytest.mark.asyncio
async def test_close_all_targets_generic_exception(
    cdp: EdgeCDPClient, monkeypatch,
) -> None:
    """A non-HTTPError exception in the close loop is caught
    and logged (best-effort), nothing closed -> False."""
    monkeypatch.setattr(
        cdp, "list_targets",
        lambda: [{"id": "T1"}])

    import urllib.request as _ur

    def _boom(*a, **k):
        raise RuntimeError("socket exploded")

    monkeypatch.setattr(_ur, "urlopen", _boom)
    out = await cdp.close_all_targets(log_prefix="auth")
    assert out is False


@pytest.mark.asyncio
async def test_await_nav_result_remaining_le_zero() -> None:
    """First iteration passes the while check, then
    `remaining <= 0` triggers the inner break with no ack ->
    False."""
    import unifideck.auth.edge_browser.cdp_client as mod

    loop = asyncio.get_event_loop()
    base = loop.time()
    # deadline just barely in the future so the while passes
    # once, but `remaining` computed next is <= 0
    times = iter([base + 0.0001, base + 10, base + 10])

    real_time = loop.time

    class _Clock:
        def time(self) -> float:
            try:
                return next(times)
            except StopIteration:
                return real_time()

    monkeypatch_loop = _Clock()

    class _WS:
        async def recv(self) -> str:
            return json.dumps({"id": 99})

    import unittest.mock as _m
    with _m.patch.object(
        mod.asyncio, "get_event_loop",
        lambda: monkeypatch_loop,
    ):
        out = await _await_navigation_result(
            _WS(), base + 0.0001, "https://x")
    assert out is False
