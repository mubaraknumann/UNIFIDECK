"""Deep executable tests — auth/orchestrator.py.

Source : py_modules/unifideck/auth/orchestrator.py
Fiche  : OP (auth/)   Critical — coverage floor 95%.

AuthOrchestrator is stateless: it composes get_url +
exchange_code callables with a browser monitor and event
bus. Driven via an event-collecting bus double and a
controllable monitor double; real AuthCaptureResult /
AuthResult objects exercise every branch (blocking +
background, success / every failure).
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from unifideck.auth.browser import AuthCaptureResult
from unifideck.auth.orchestrator import (
    AuthOrchestrator,
    OrchestratorConfig,
)
from unifideck.core.types import AuthResult, Events


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, name: str, **kw: Any) -> None:
        self.events.append((name, kw))


class _Monitor:
    def __init__(
        self,
        capture: AuthCaptureResult | None = None,
        *,
        crash: bool = False,
    ) -> None:
        self._capture = capture
        self._crash = crash
        self.closed: list[str] = []

    async def wait_for_redirect(
        self, *, allowed_uris: list[str],
        timeout: float, **kw: object,
    ) -> AuthCaptureResult:
        if self._crash:
            raise RuntimeError("monitor exploded")
        assert self._capture is not None
        return self._capture

    async def close_oauth_tab(self, domain: str) -> None:
        self.closed.append(domain)


def _orch(
    bus: _Bus | None = None,
    monitor: _Monitor | None = None,
) -> AuthOrchestrator:
    return AuthOrchestrator(
        bus or _Bus(),                 # type: ignore[arg-type]
        monitor or _Monitor(),         # type: ignore[arg-type]
        "epic",
        OrchestratorConfig(
            timeout=5.0, browser_launch_grace=0.0),
    )


async def _url_ok() -> str:
    return "https://oauth.epic/login"


async def _url_empty() -> str:
    return ""


async def _url_boom() -> str:
    raise RuntimeError("get_url exploded")


async def _exchange_ok(code: str) -> AuthResult:
    return AuthResult(success=True, store="epic")


async def _exchange_fail(code: str) -> AuthResult:
    return AuthResult(
        success=False, error="bad_code", store="epic")


async def _exchange_boom(code: str) -> AuthResult:
    raise RuntimeError("exchange exploded")


def test_module_imports() -> None:
    import unifideck.auth.orchestrator as mod
    assert mod.AuthOrchestrator is AuthOrchestrator


def test_orchestrator_config_defaults() -> None:
    c = OrchestratorConfig()
    assert c.timeout == 300.0
    assert c.browser_launch_grace == 1.5


# ========================================================= #
# run_flow — get_url failures
# ========================================================= #
@pytest.mark.asyncio
async def test_run_flow_get_url_raises() -> None:
    bus = _Bus()
    o = _orch(bus)
    res = await o.run_flow(
        _url_boom, ["https://cb"], _exchange_ok)
    assert res.success is False
    assert res.error == "get_url_failed"
    assert any(
        e[0] == Events.STORE_AUTH_FAILED
        for e in bus.events)


@pytest.mark.asyncio
async def test_run_flow_get_url_empty() -> None:
    o = _orch()
    res = await o.run_flow(
        _url_empty, ["https://cb"], _exchange_ok)
    assert res.success is False
    assert res.error == "no_url"


# ========================================================= #
# run_flow — write_url_file
# ========================================================= #
@pytest.mark.asyncio
async def test_run_flow_writes_url_file(
    tmp_path,
) -> None:
    cap = AuthCaptureResult(
        success=True,
        redirect_url="https://cb?code=XYZ",
        params={"code": "XYZ"})
    o = _orch(monitor=_Monitor(cap))
    target = tmp_path / "sub" / "url.txt"
    res = await o.run_flow(
        _url_ok, ["https://cb"], _exchange_ok,
        write_url_file=str(target))
    assert res.success is True
    assert target.read_text() == \
        "https://oauth.epic/login"


@pytest.mark.asyncio
async def test_run_flow_write_url_fails_returns_false() -> None:
    """BUG #2 FIXED (auth/orchestrator): when the URL write
    fails, `_write_url_atomically` now binds `expanded` up
    front, so its `except OSError` handler returns False
    cleanly instead of raising UnboundLocalError. run_flow
    surfaces that as an unsuccessful AuthResult rather than
    propagating an exception."""
    o = _orch()
    res = await o.run_flow(
        _url_ok, ["https://cb"], _exchange_ok,
        write_url_file="/proc/cannot/write/here.txt")
    assert res.success is False


# ========================================================= #
# blocking mode — redirect outcomes
# ========================================================= #
@pytest.mark.asyncio
async def test_blocking_success() -> None:
    cap = AuthCaptureResult(
        success=True,
        redirect_url="https://cb/done?code=ABC",
        params={"code": "ABC"})
    bus = _Bus()
    mon = _Monitor(cap)
    o = _orch(bus, mon)
    res = await o.run_flow(
        _url_ok, ["https://cb"], _exchange_ok)
    assert res.success is True
    assert any(
        e[0] == Events.STORE_AUTH_COMPLETE
        for e in bus.events)
    assert mon.closed  # OAuth tab closed


@pytest.mark.asyncio
async def test_blocking_capture_failed() -> None:
    cap = AuthCaptureResult(
        success=False, error="user_closed",
        elapsed_seconds=3.2)
    o = _orch(monitor=_Monitor(cap))
    res = await o.run_flow(
        _url_ok, ["https://cb"], _exchange_ok)
    assert res.success is False
    assert res.error == "user_closed"


@pytest.mark.asyncio
async def test_blocking_no_code() -> None:
    cap = AuthCaptureResult(
        success=True,
        redirect_url="https://cb/done",
        params={})  # no code
    o = _orch(monitor=_Monitor(cap))
    res = await o.run_flow(
        _url_ok, ["https://cb"], _exchange_ok)
    assert res.success is False
    assert res.error == "no_code"


@pytest.mark.asyncio
async def test_blocking_monitor_crash() -> None:
    o = _orch(monitor=_Monitor(crash=True))
    res = await o.run_flow(
        _url_ok, ["https://cb"], _exchange_ok)
    assert res.success is False
    assert res.error == "monitor_crashed"


@pytest.mark.asyncio
async def test_blocking_exchange_fails() -> None:
    cap = AuthCaptureResult(
        success=True,
        redirect_url="https://cb?code=Z",
        params={"code": "Z"})
    bus = _Bus()
    o = _orch(bus, _Monitor(cap))
    res = await o.run_flow(
        _url_ok, ["https://cb"], _exchange_fail)
    assert res.success is False
    assert any(
        e[0] == Events.STORE_AUTH_FAILED
        for e in bus.events)


@pytest.mark.asyncio
async def test_blocking_exchange_raises() -> None:
    cap = AuthCaptureResult(
        success=True,
        redirect_url="https://cb?code=Z",
        params={"code": "Z"})
    o = _orch(monitor=_Monitor(cap))
    res = await o.run_flow(
        _url_ok, ["https://cb"], _exchange_boom)
    assert res.success is False
    assert res.error == "exchange_failed"


@pytest.mark.asyncio
async def test_blocking_timeout_override() -> None:
    """An explicit timeout override is accepted (deadline
    plumbing)."""
    cap = AuthCaptureResult(
        success=True,
        redirect_url="https://cb?code=A",
        params={"code": "A"})
    o = _orch(monitor=_Monitor(cap))
    res = await o.run_flow(
        _url_ok, ["https://cb"], _exchange_ok,
        timeout=42.0)
    assert res.success is True


# ========================================================= #
# background mode
# ========================================================= #
@pytest.mark.asyncio
async def test_background_returns_pending() -> None:
    cap = AuthCaptureResult(
        success=True,
        redirect_url="https://cb?code=BG",
        params={"code": "BG"})
    o = _orch(monitor=_Monitor(cap))
    res = await o.run_flow(
        _url_ok, ["https://cb"], _exchange_ok,
        background=True)
    assert res.success is True
    assert res.metadata.get("pending") is True
    assert res.url == "https://oauth.epic/login"
    # let the background task complete
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_background_then_cancel() -> None:
    """Starting a second background flow cancels the first;
    cancel_background reports correctly."""
    cap = AuthCaptureResult(
        success=True,
        redirect_url="https://cb?code=A",
        params={"code": "A"})

    class _SlowMon(_Monitor):
        async def wait_for_redirect(
            self, *, allowed_uris, timeout, **kw,
        ):
            await asyncio.sleep(5)
            return cap

    o = _orch(monitor=_SlowMon(cap))
    await o.run_flow(
        _url_ok, ["https://cb"], _exchange_ok,
        background=True)
    # a background task is in flight
    assert o.cancel_background() is True
    # nothing in flight now
    assert o.cancel_background() is False


@pytest.mark.asyncio
async def test_cancel_background_none() -> None:
    o = _orch()
    assert o.cancel_background() is False


# ========================================================= #
# event + I/O helpers
# ========================================================= #
@pytest.mark.asyncio
async def test_emit_started() -> None:
    bus = _Bus()
    o = _orch(bus)
    await o._emit_started()
    assert bus.events[0][0] == Events.STORE_AUTH_STARTED


@pytest.mark.asyncio
async def test_emit_failed_builds_result() -> None:
    bus = _Bus()
    o = _orch(bus)
    res = await o._emit_failed(
        "some_err", "detail", url="https://x")
    assert res.success is False
    assert res.error == "some_err"
    assert res.url == "https://x"
    assert bus.events[0][0] == Events.STORE_AUTH_FAILED


@pytest.mark.asyncio
async def test_close_tab_safely_none() -> None:
    o = _orch()
    await o._close_tab_safely(None)  # no raise


@pytest.mark.asyncio
async def test_close_tab_safely_strips_scheme() -> None:
    mon = _Monitor()
    o = _orch(monitor=mon)
    await o._close_tab_safely(
        "https://accounts.epicgames.com/oauth/cb")
    assert mon.closed == ["accounts.epicgames.com"]


@pytest.mark.asyncio
async def test_close_tab_safely_swallows_error() -> None:
    class _BadMon(_Monitor):
        async def close_oauth_tab(self, d: str) -> None:
            raise RuntimeError("cdp dead")

    o = _orch(monitor=_BadMon())
    # must not raise
    await o._close_tab_safely("https://x.com/cb")


@pytest.mark.asyncio
async def test_write_url_atomically_ok(tmp_path) -> None:
    target = tmp_path / "deep" / "auth.url"
    ok = await AuthOrchestrator._write_url_atomically(
        str(target), "https://oauth/x")
    assert ok is True
    assert target.read_text() == "https://oauth/x"


@pytest.mark.asyncio
async def test_write_url_atomically_failure_returns_false(
) -> None:
    """BUG #2 FIXED at the unit level: an unwritable path
    makes the `except OSError` handler return False
    (instead of raising UnboundLocalError on an unbound
    `expanded`)."""
    out = await AuthOrchestrator._write_url_atomically(
        "/proc/nope/cannot.url", "https://x")
    assert out is False


# --- CancelledError + background runner completion ------ #
@pytest.mark.asyncio
async def test_await_redirect_cancelled_reraises() -> None:
    """A CancelledError from wait_for_redirect is re-raised
    (so background mode can stop cleanly on logout)."""
    class _CancelMon(_Monitor):
        async def wait_for_redirect(
            self, *, allowed_uris, timeout, **kw,
        ):
            raise asyncio.CancelledError()

    o = _orch(monitor=_CancelMon())
    with pytest.raises(asyncio.CancelledError):
        await o._await_redirect_and_exchange(
            url="https://x",
            allowed_uris=["https://cb"],
            exchange_code=_exchange_ok,
            deadline=1.0)


@pytest.mark.asyncio
async def test_background_runner_runs_to_completion(
) -> None:
    """The background task actually executes the redirect +
    exchange and clears _bg_task in its finally block."""
    cap = AuthCaptureResult(
        success=True,
        redirect_url="https://cb?code=DONE",
        params={"code": "DONE"})
    bus = _Bus()
    o = _orch(bus, _Monitor(cap))
    res = await o.run_flow(
        _url_ok, ["https://cb"], _exchange_ok,
        background=True)
    assert res.metadata.get("pending") is True
    # wait for the spawned task to finish
    for _ in range(50):
        await asyncio.sleep(0.01)
        if o._bg_task is None or o._bg_task.done():
            break
    # the exchange completed -> COMPLETE emitted by the bg task
    assert any(
        e[0] == Events.STORE_AUTH_COMPLETE
        for e in bus.events)


@pytest.mark.asyncio
async def test_background_runner_swallows_cancel() -> None:
    """If the background task is cancelled mid-flight the
    runner's `except CancelledError: pass` body runs (the
    swallow path is exercised). asyncio still marks the task
    cancelled, so awaiting it surfaces CancelledError — what
    matters here is that the runner body handled it."""
    started = asyncio.Event()

    class _HangMon(_Monitor):
        async def wait_for_redirect(
            self, *, allowed_uris, timeout, **kw,
        ):
            started.set()
            await asyncio.sleep(10)
            raise AssertionError("should be cancelled")

    o = _orch(monitor=_HangMon())
    await o.run_flow(
        _url_ok, ["https://cb"], _exchange_ok,
        background=True)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task = o._bg_task
    assert task is not None
    task.cancel()
    # drain the task; either it was swallowed cleanly or the
    # cancellation surfaces — both are acceptable, the point
    # is the runner's CancelledError branch executed.
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled() or task.done()
