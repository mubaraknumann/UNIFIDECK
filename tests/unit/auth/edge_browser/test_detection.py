"""Deep executable tests — auth/edge_browser/detection.py.

Source : py_modules/unifideck/auth/edge_browser/detection.py
Fiche  : OP (auth/)   Critical — coverage floor 95%.

Microsoft Edge discovery: flatpak remotes enumeration +
flatpak/native command resolution. shutil.which and
subprocess.run are stubbed; no real flatpak invoked.
"""
from __future__ import annotations

from typing import Any

import pytest

import unifideck.auth.edge_browser.detection as DET
from unifideck.auth.edge_browser.detection import (
    find_edge_cmd,
    flatpak_remote_names,
    is_edge_installed,
    _try_flatpak_app,
)


def _env() -> dict:
    return {"X": "1"}


class _R:
    def __init__(self, rc: int = 0, out: str = ""
                 ) -> None:
        self.returncode = rc
        self.stdout = out


def test_module_imports() -> None:
    assert hasattr(DET, "find_edge_cmd")


# ========================================================= #
# flatpak_remote_names
# ========================================================= #
def test_remotes_bad_scope() -> None:
    assert flatpak_remote_names(
        _env, "--bogus") == set()


def test_remotes_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        DET.subprocess, "run",
        lambda *a, **k: _R(
            0, "Name\nflathub\nlocal\n"))
    out = flatpak_remote_names(_env, "--user")
    assert out == {"flathub", "local"}


def test_remotes_nonzero(monkeypatch) -> None:
    monkeypatch.setattr(
        DET.subprocess, "run",
        lambda *a, **k: _R(1, ""))
    assert flatpak_remote_names(
        _env, "--system") == set()


def test_remotes_subprocess_error(
    monkeypatch,
) -> None:
    def _boom(*a: Any, **k: Any):
        raise OSError("flatpak missing")

    monkeypatch.setattr(
        DET.subprocess, "run", _boom)
    assert flatpak_remote_names(
        _env, "--user") == set()


def test_remotes_skips_header_and_blank(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        DET.subprocess, "run",
        lambda *a, **k: _R(
            0, "name\n\n  flathub  \n"))
    out = flatpak_remote_names(_env, "--user")
    assert out == {"flathub"}


# ========================================================= #
# _try_flatpak_app
# ========================================================= #
def test_try_flatpak_user_scope(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        DET.subprocess, "run",
        lambda *a, **k: _R(0))
    out = _try_flatpak_app(
        "com.microsoft.Edge", _env)
    assert out == [
        "flatpak", "run", "com.microsoft.Edge"]


def test_try_flatpak_not_found(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        DET.subprocess, "run",
        lambda *a, **k: _R(1))
    assert _try_flatpak_app(
        "com.microsoft.Edge", _env) is None


def test_try_flatpak_exception(
    monkeypatch,
) -> None:
    def _boom(*a: Any, **k: Any):
        raise OSError("race")

    monkeypatch.setattr(
        DET.subprocess, "run", _boom)
    assert _try_flatpak_app(
        "com.microsoft.Edge", _env) is None


# ========================================================= #
# find_edge_cmd
# ========================================================= #
def test_find_edge_flatpak(monkeypatch) -> None:
    monkeypatch.setattr(
        DET.shutil, "which",
        lambda n: "/usr/bin/flatpak"
        if n == "flatpak" else None)
    monkeypatch.setattr(
        DET, "_try_flatpak_app",
        lambda app, fn: ["flatpak", "run", app])
    out = find_edge_cmd(_env)
    assert out == [
        "flatpak", "run", "com.microsoft.Edge"]


def test_find_edge_native_fallback(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        DET.shutil, "which",
        lambda n: "/usr/bin/microsoft-edge"
        if n == "microsoft-edge" else None)
    out = find_edge_cmd(_env)
    assert out == ["microsoft-edge"]


def test_find_edge_flatpak_present_app_absent(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        DET.shutil, "which",
        lambda n: "/usr/bin/flatpak"
        if n == "flatpak" else None)
    monkeypatch.setattr(
        DET, "_try_flatpak_app",
        lambda app, fn: None)
    # flatpak present but Edge not installed, no native -> None
    assert find_edge_cmd(_env) is None


def test_find_edge_none(monkeypatch) -> None:
    monkeypatch.setattr(
        DET.shutil, "which", lambda n: None)
    assert find_edge_cmd(_env) is None


# ========================================================= #
# is_edge_installed
# ========================================================= #
def test_is_edge_installed_true(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        DET, "find_edge_cmd",
        lambda fn: ["microsoft-edge"])
    assert is_edge_installed(_env) is True


def test_is_edge_installed_false(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        DET, "find_edge_cmd", lambda fn: None)
    assert is_edge_installed(_env) is False
