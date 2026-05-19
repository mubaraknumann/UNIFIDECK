"""Deep behavioural tests — auth/browser_content.py.

Source : py_modules/unifideck/auth/browser_content.py
New module extracted from auth/browser.py in the restructure
(lot 13a file-cap split). Covers CDP page-content code
extraction: log-level escalation, regex matching, the
websocket inner-text eval (success / timeout / empty / no
ws-url), the Epic JSON-blob path, and the generic
content-trigger fallback.

The CDP websocket is faked end-to-end (connect → send → recv)
so no real browser or network is involved.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

import unifideck.auth.browser_content as BC
from unifideck.auth.browser_content import (
    extract_code_from_page,
    log_extract,
    match_pattern_in_text,
    try_content_fallback,
    try_epic_content_capture,
)


# --------------------------------------------------------- #
# Fake CDP websocket
# --------------------------------------------------------- #
class _FakeWS:
    def __init__(
        self,
        *,
        recv_value: Any = None,
        recv_raises: type[BaseException] | None = None,
    ) -> None:
        self._recv_value = recv_value
        self._recv_raises = recv_raises
        self.sent: list[str] = []

    async def __aenter__(self) -> "_FakeWS":
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> Any:
        if self._recv_raises is not None:
            raise self._recv_raises()
        return self._recv_value


def _install_fake_ws(monkeypatch, ws: _FakeWS) -> None:
    import sys
    import types

    fake = types.ModuleType("websockets")

    def _connect(url: str, **kw: Any) -> _FakeWS:
        ws.url = url  # type: ignore[attr-defined]
        return ws

    fake.connect = _connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "websockets", fake)


def _cdp_text_response(value: str) -> str:
    return json.dumps(
        {"id": 1, "result": {"result": {"value": value}}})


# ========================================================= #
# log_extract
# ========================================================= #
def test_log_extract_first_attempt_info(caplog) -> None:
    import logging

    with caplog.at_level(logging.INFO,
                         logger="unifideck.auth.browser_content"):
        log_extract(True, "hello %s", "world")
    assert any("hello world" in r.message
               for r in caplog.records)


def test_log_extract_retry_is_debug(caplog) -> None:
    import logging

    caplog.set_level(
        logging.INFO,
        logger="unifideck.auth.browser_content")
    log_extract(False, "routine %s", "poll")
    # emitted at DEBUG -> not captured at INFO level
    assert not any("routine poll" in r.message
                   for r in caplog.records)


# ========================================================= #
# match_pattern_in_text
# ========================================================= #
def test_match_pattern_found() -> None:
    out = match_pattern_in_text(
        'x "authorizationCode": "ABC123" y',
        r'"authorizationCode"\s*:\s*"([^"]+)"',
        "http://x", True)
    assert out == "ABC123"


def test_match_pattern_not_found() -> None:
    out = match_pattern_in_text(
        "no code here",
        r'code=([0-9]+)', "http://x", False)
    assert out is None


def test_match_pattern_first_group_only() -> None:
    out = match_pattern_in_text(
        "v=42&w=99",
        r"v=(\d+)&w=(\d+)", "http://x", True)
    assert out == "42"


# ========================================================= #
# cdp_eval_inner_text  (via extract_code_from_page)
# ========================================================= #
@pytest.mark.asyncio
async def test_extract_code_success(monkeypatch) -> None:
    ws = _FakeWS(recv_value=_cdp_text_response(
        'blah "authorizationCode": "TOK-9" blah'))
    _install_fake_ws(monkeypatch, ws)
    code = await extract_code_from_page(
        {"webSocketDebuggerUrl": "ws://x",
         "url": "http://epic/redirect"},
        r'"authorizationCode"\s*:\s*"([^"]+)"',
        first_attempt=True)
    assert code == "TOK-9"
    # the Runtime.evaluate request was sent
    assert "Runtime.evaluate" in ws.sent[0]


@pytest.mark.asyncio
async def test_extract_code_no_ws_url(monkeypatch) -> None:
    code = await extract_code_from_page(
        {"url": "http://x"}, r"code=(\d+)")
    assert code is None


@pytest.mark.asyncio
async def test_extract_code_recv_timeout(
    monkeypatch,
) -> None:
    ws = _FakeWS(recv_raises=TimeoutError)
    _install_fake_ws(monkeypatch, ws)
    code = await extract_code_from_page(
        {"webSocketDebuggerUrl": "ws://x",
         "url": "http://x"},
        r"code=(\d+)", first_attempt=True)
    assert code is None


@pytest.mark.asyncio
async def test_extract_code_empty_value(
    monkeypatch,
) -> None:
    ws = _FakeWS(recv_value=_cdp_text_response(""))
    _install_fake_ws(monkeypatch, ws)
    code = await extract_code_from_page(
        {"webSocketDebuggerUrl": "ws://x",
         "url": "http://x"},
        r"code=(\d+)")
    assert code is None


@pytest.mark.asyncio
async def test_extract_code_connection_error(
    monkeypatch,
) -> None:
    import sys
    import types

    fake = types.ModuleType("websockets")

    def _connect(url: str, **kw: Any):
        raise OSError("connection refused")

    fake.connect = _connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "websockets", fake)
    code = await extract_code_from_page(
        {"webSocketDebuggerUrl": "ws://x",
         "url": "http://x"},
        r"code=(\d+)", first_attempt=True)
    assert code is None


@pytest.mark.asyncio
async def test_extract_code_missing_value_chain(
    monkeypatch,
) -> None:
    ws = _FakeWS(recv_value=json.dumps(
        {"id": 1, "result": {}}))
    _install_fake_ws(monkeypatch, ws)
    code = await extract_code_from_page(
        {"webSocketDebuggerUrl": "ws://x",
         "url": "http://x"},
        r"code=(\d+)")
    assert code is None


# ========================================================= #
# try_epic_content_capture
# ========================================================= #
@pytest.mark.asyncio
async def test_epic_capture_success(monkeypatch) -> None:
    ws = _FakeWS(recv_value=_cdp_text_response(
        '{"authorizationCode": "EPIC-CODE-1"}'))
    _install_fake_ws(monkeypatch, ws)
    state: dict[str, Any] = {
        "content_extract_first_attempt": set()}
    res = await try_epic_content_capture(
        {"webSocketDebuggerUrl": "ws://x",
         "url": "http://epic/redirect"},
        "http://epic/redirect", state, 0.0)
    assert res is not None
    assert res.success is True
    assert res.params["code"] == "EPIC-CODE-1"
    # url recorded so a retry logs at DEBUG
    assert "http://epic/redirect" in \
        state["content_extract_first_attempt"]


@pytest.mark.asyncio
async def test_epic_capture_no_code(monkeypatch) -> None:
    ws = _FakeWS(recv_value=_cdp_text_response(
        "login form, no code yet"))
    _install_fake_ws(monkeypatch, ws)
    state: dict[str, Any] = {
        "content_extract_first_attempt": set()}
    res = await try_epic_content_capture(
        {"webSocketDebuggerUrl": "ws://x",
         "url": "http://epic/redirect"},
        "http://epic/redirect", state, 0.0)
    assert res is None
    # still recorded (first attempt consumed)
    assert "http://epic/redirect" in \
        state["content_extract_first_attempt"]


@pytest.mark.asyncio
async def test_epic_capture_extract_raises(
    monkeypatch,
) -> None:
    async def _boom(*a: Any, **k: Any):
        raise RuntimeError("cdp exploded")

    monkeypatch.setattr(
        BC, "extract_code_from_page", _boom)
    state: dict[str, Any] = {
        "content_extract_first_attempt": set()}
    res = await try_epic_content_capture(
        {"url": "http://epic/redirect"},
        "http://epic/redirect", state, 0.0)
    assert res is None


# ========================================================= #
# try_content_fallback
# ========================================================= #
@pytest.mark.asyncio
async def test_content_fallback_disabled_when_no_args(
) -> None:
    out = await try_content_fallback(
        [{"url": "http://x"}], None, None, 0.0)
    assert out is None


@pytest.mark.asyncio
async def test_content_fallback_success(
    monkeypatch,
) -> None:
    async def _extract(target, regex, **kw: Any):
        return "FALLBACK-CODE"

    monkeypatch.setattr(
        BC, "extract_code_from_page", _extract)
    out = await try_content_fallback(
        [{"url": "http://provider/cb?x=1"}],
        "provider/cb", r"code=([A-Z-]+)", 0.0)
    assert out is not None
    assert out.params["code"] == "FALLBACK-CODE"


@pytest.mark.asyncio
async def test_content_fallback_no_matching_target(
    monkeypatch,
) -> None:
    out = await try_content_fallback(
        [{"url": "http://other/page"}],
        "provider/cb", r"code=(\d+)", 0.0)
    assert out is None


@pytest.mark.asyncio
async def test_content_fallback_extract_raises(
    monkeypatch,
) -> None:
    async def _boom(*a: Any, **k: Any):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        BC, "extract_code_from_page", _boom)
    out = await try_content_fallback(
        [{"url": "http://provider/cb"}],
        "provider/cb", r"code=(\d+)", 0.0)
    assert out is None


@pytest.mark.asyncio
async def test_content_fallback_empty_code_skips(
    monkeypatch,
) -> None:
    async def _extract(target, regex, **kw: Any):
        return None

    monkeypatch.setattr(
        BC, "extract_code_from_page", _extract)
    out = await try_content_fallback(
        [{"url": "http://provider/cb"}],
        "provider/cb", r"code=(\d+)", 0.0)
    assert out is None
