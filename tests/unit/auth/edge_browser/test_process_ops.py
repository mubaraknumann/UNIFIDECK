"""Deep executable tests — auth/edge_browser/process_ops.py.

Source : py_modules/unifideck/auth/edge_browser/process_ops.py
Fiche  : OP (auth/)   Critical — coverage floor 95%.

Process-group lifecycle helpers for the Edge auth browser:
graceful SIGTERM→SIGKILL escalation and startup-crash
detection. os.killpg / os.getpgid / time.sleep stubbed.
"""
from __future__ import annotations

import signal
import subprocess
from typing import Any

import pytest

import unifideck.auth.edge_browser.process_ops as PO
from unifideck.auth.edge_browser.process_ops import (
    _force_kill,
    _log_crash_tail,
    _safe_getpgid,
    _signal_group_or_single,
    graceful_kill,
    wait_and_check_crash,
)


class _Proc:
    def __init__(
        self,
        *,
        pid: int = 4242,
        wait_raises: bool = False,
        poll_seq: list | None = None,
    ) -> None:
        self.pid = pid
        self._wait_raises = wait_raises
        self._poll_seq = list(poll_seq or [])
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None
             ) -> int:
        if self._wait_raises:
            raise subprocess.TimeoutExpired(
                "edge", timeout or 0)
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def poll(self) -> int | None:
        if self._poll_seq:
            return self._poll_seq.pop(0)
        return None


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    import time as _t

    monkeypatch.setattr(_t, "sleep", lambda s: None)
    yield


def test_module_imports() -> None:
    assert hasattr(PO, "graceful_kill")


# ========================================================= #
# _safe_getpgid
# ========================================================= #
def test_safe_getpgid_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        PO.os, "getpgid", lambda pid: 999)
    assert _safe_getpgid(1234) == 999


def test_safe_getpgid_error(monkeypatch) -> None:
    def _boom(pid):
        raise ProcessLookupError()

    monkeypatch.setattr(PO.os, "getpgid", _boom)
    assert _safe_getpgid(1234) is None


# ========================================================= #
# _signal_group_or_single
# ========================================================= #
def test_signal_group(monkeypatch) -> None:
    sent = {}
    monkeypatch.setattr(
        PO, "_safe_getpgid", lambda pid: 555)
    monkeypatch.setattr(
        PO.os, "getpgrp", lambda: 1)
    monkeypatch.setattr(
        PO.os, "killpg",
        lambda pg, s: sent.update(
            {"pg": pg, "s": s}))
    _signal_group_or_single(
        _Proc(), signal.SIGTERM)
    assert sent == {"pg": 555,
                    "s": signal.SIGTERM}


def test_signal_single_sigterm(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        PO, "_safe_getpgid", lambda pid: None)
    p = _Proc()
    _signal_group_or_single(p, signal.SIGTERM)
    assert p.terminated is True


def test_signal_single_sigkill(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        PO, "_safe_getpgid", lambda pid: None)
    p = _Proc()
    _signal_group_or_single(p, signal.SIGKILL)
    assert p.killed is True


def test_signal_own_group_falls_back(
    monkeypatch,
) -> None:
    """pgid == own process group -> don't killpg, fall
    back to terminate."""
    monkeypatch.setattr(
        PO, "_safe_getpgid", lambda pid: 42)
    monkeypatch.setattr(
        PO.os, "getpgrp", lambda: 42)
    p = _Proc()
    _signal_group_or_single(p, signal.SIGTERM)
    assert p.terminated is True


# ========================================================= #
# _force_kill
# ========================================================= #
def test_force_kill_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        PO, "_signal_group_or_single",
        lambda p, s: None)
    _force_kill(_Proc())  # no raise


def test_force_kill_swallows_error(
    monkeypatch,
) -> None:
    def _boom(p, s):
        raise OSError("kill failed")

    monkeypatch.setattr(
        PO, "_signal_group_or_single", _boom)
    _force_kill(_Proc())  # swallowed


# ========================================================= #
# graceful_kill
# ========================================================= #
def test_graceful_kill_none() -> None:
    graceful_kill(None)  # no raise


def test_graceful_kill_clean(monkeypatch) -> None:
    monkeypatch.setattr(
        PO, "_signal_group_or_single",
        lambda p, s: None)
    p = _Proc()
    graceful_kill(p)  # wait() returns 0, no escalation


def test_graceful_kill_timeout_escalates(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        PO, "_signal_group_or_single",
        lambda p, s: None)
    forced = {"v": False}
    monkeypatch.setattr(
        PO, "_force_kill",
        lambda p: forced.update({"v": True}))
    p = _Proc(wait_raises=True)
    graceful_kill(p)
    assert forced["v"] is True


def test_graceful_kill_generic_error(
    monkeypatch,
) -> None:
    def _boom(p, s):
        raise RuntimeError("signal boom")

    monkeypatch.setattr(
        PO, "_signal_group_or_single", _boom)
    graceful_kill(_Proc())  # swallowed (debug log)


# ========================================================= #
# _log_crash_tail
# ========================================================= #
def test_log_crash_tail_ok(tmp_path) -> None:
    lf = tmp_path / "edge.log"
    lf.write_text("crash details here")
    _log_crash_tail(str(lf))  # reads + logs, no raise


def test_log_crash_tail_missing() -> None:
    _log_crash_tail("/no/such/log")  # swallowed


# ========================================================= #
# wait_and_check_crash
# ========================================================= #
@pytest.mark.asyncio
async def test_wait_crash_none() -> None:
    assert await wait_and_check_crash(
        None, lambda: True, "/log") is False


@pytest.mark.asyncio
async def test_wait_crash_cdp_responsive(
    monkeypatch,
) -> None:
    async def _fast_sleep(_s):
        return None

    monkeypatch.setattr(
        PO.asyncio, "sleep", _fast_sleep)
    p = _Proc(poll_seq=[None])
    out = await wait_and_check_crash(
        p, lambda: True, "/log")
    assert out is True


@pytest.mark.asyncio
async def test_wait_crash_process_exits(
    monkeypatch, tmp_path,
) -> None:
    async def _fast_sleep(_s):
        return None

    monkeypatch.setattr(
        PO.asyncio, "sleep", _fast_sleep)
    lf = tmp_path / "edge.log"
    lf.write_text("boom")
    p = _Proc(poll_seq=[0])  # exited
    out = await wait_and_check_crash(
        p, lambda: False, str(lf))
    assert out is False


@pytest.mark.asyncio
async def test_wait_crash_timeout_alive(
    monkeypatch,
) -> None:
    async def _fast_sleep(_s):
        return None

    monkeypatch.setattr(
        PO.asyncio, "sleep", _fast_sleep)
    # poll always None (alive), probe always False ->
    # exhausts loop, returns True (let caller retry)
    p = _Proc(poll_seq=[])
    out = await wait_and_check_crash(
        p, lambda: False, "/log")
    assert out is True
