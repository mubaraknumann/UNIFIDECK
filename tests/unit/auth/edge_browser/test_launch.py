"""Deep executable tests — auth/edge_browser/launch.py.

Source : py_modules/unifideck/auth/edge_browser/launch.py
Fiche  : OP (auth/)   Critical — coverage floor 95%.

Edge launch helpers (auth + xCloud). The EdgeBrowser is a
controllable double; subprocess.Popen and PROFILE_DIR are
stubbed so no real browser is spawned.
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.auth.edge_browser import launch as L


class _Browser:
    def __init__(
        self,
        *,
        cmd: list[str] | None = None,
    ) -> None:
        self._cmd = cmd
        self.cdp_port = 9222
        self.process: Any = None
        self.killed = False
        self.cleaned = False

    def kill(self) -> None:
        self.killed = True

    def cleanup_stale_profile_state(self) -> None:
        self.cleaned = True

    def find_cmd(self) -> list[str] | None:
        return self._cmd

    def locale_fn(self) -> str:
        return "fr-FR"


@pytest.fixture(autouse=True)
def _profile_dir(tmp_path, monkeypatch):
    """Make PROFILE_DIR / LOG_FILE point at tmp_path so the
    lazy `from .edge import ...` inside the functions resolves
    to writable paths."""
    import unifideck.auth.edge_browser.edge as edge

    monkeypatch.setattr(
        edge, "PROFILE_DIR",
        str(tmp_path / "profile"), raising=False)
    monkeypatch.setattr(
        edge, "LOG_FILE",
        str(tmp_path / "edge.log"), raising=False)
    yield


def test_module_imports() -> None:
    assert hasattr(L, "launch_auth")
    assert hasattr(L, "launch_xcloud")


# ========================================================= #
# _prepare_for_launch
# ========================================================= #
def test_prepare_no_cmd() -> None:
    b = _Browser(cmd=None)
    assert L._prepare_for_launch(b) is None
    assert b.killed is True
    assert b.cleaned is True


def test_prepare_ok(tmp_path) -> None:
    b = _Browser(cmd=["flatpak", "run", "edge"])
    out = L._prepare_for_launch(b)
    assert out == ["flatpak", "run", "edge"]


# ========================================================= #
# _spawn_edge_process
# ========================================================= #
def test_spawn_success(monkeypatch) -> None:
    b = _Browser()

    class _Popen:
        pid = 4321

        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        # Source does `subprocess.Popen[bytes](...)` — the
        # type subscription must resolve on the stub too.
        def __class_getitem__(cls, _item):
            return cls

    monkeypatch.setattr(
        L.subprocess, "Popen", _Popen)
    ok = L._spawn_edge_process(
        b, ["edge", "--app=x"], "w", "Auth")
    assert ok is True
    assert b.process.pid == 4321


def test_spawn_popen_error(monkeypatch) -> None:
    b = _Browser()

    def _boom(*a: Any, **k: Any) -> Any:
        raise OSError("exec failed")

    monkeypatch.setattr(
        L.subprocess, "Popen", _boom)
    ok = L._spawn_edge_process(
        b, ["edge"], "w", "Auth")
    assert ok is False


def test_spawn_log_open_fails(
    monkeypatch, tmp_path,
) -> None:
    """If the log file can't be opened the spawn still
    proceeds with DEVNULL."""
    b = _Browser()
    import unifideck.auth.edge_browser.edge as edge

    # point LOG_FILE at an unwritable path (parent is a file)
    blocker = tmp_path / "blk"
    blocker.write_text("x")
    monkeypatch.setattr(
        edge, "LOG_FILE",
        str(blocker / "edge.log"), raising=False)

    class _Popen:
        pid = 1

        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def __class_getitem__(cls, _item):
            return cls

    monkeypatch.setattr(
        L.subprocess, "Popen", _Popen)
    ok = L._spawn_edge_process(
        b, ["edge"], "w", "Auth")
    assert ok is True


# ========================================================= #
# launch_auth
# ========================================================= #
def test_launch_auth_no_browser() -> None:
    b = _Browser(cmd=None)
    assert L.launch_auth(b, "https://login") is False


def test_launch_auth_success(monkeypatch) -> None:
    b = _Browser(cmd=["edge"])
    captured = {}

    def _spawn(browser, args, log_mode, label):
        captured["args"] = args
        captured["mode"] = log_mode
        captured["label"] = label
        return True

    monkeypatch.setattr(
        L, "_spawn_edge_process", _spawn)
    ok = L.launch_auth(b, "https://login.live.com")
    assert ok is True
    assert any("--app=https://login.live.com" in a
               for a in captured["args"])
    assert any(
        "--remote-debugging-port=9222" in a
        for a in captured["args"])
    assert captured["mode"] == "w"
    assert captured["label"] == "Auth"


# ========================================================= #
# launch_xcloud
# ========================================================= #
def test_launch_xcloud_no_browser() -> None:
    b = _Browser(cmd=None)
    assert L.launch_xcloud(
        b, "https://xbox.com/play") is False


def test_launch_xcloud_success(monkeypatch) -> None:
    b = _Browser(cmd=["edge"])
    captured = {}

    def _spawn(browser, args, log_mode, label):
        captured["args"] = args
        captured["mode"] = log_mode
        captured["label"] = label
        return True

    monkeypatch.setattr(
        L, "_spawn_edge_process", _spawn)
    ok = L.launch_xcloud(
        b, "https://www.xbox.com/play/launch/X")
    assert ok is True
    assert "--kiosk" in captured["args"]
    # xcloud uses cdp_port + 1
    assert any(
        "--remote-debugging-port=9223" in a
        for a in captured["args"])
    assert captured["args"][-1] == \
        "https://www.xbox.com/play/launch/X"
    assert captured["mode"] == "a"
    assert captured["label"] == "xCloud"
