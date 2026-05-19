"""Deep executable tests — auth/edge_browser/env.py.

Source : py_modules/unifideck/auth/edge_browser/env.py
Fiche  : OP (auth/)   Critical — coverage floor 95%.

Four-stage graphical-session env discovery for launching
the Edge auth browser from Decky's headless backend. os.env,
subprocess.run and the filesystem are stubbed; every
discovery stage and fallback branch is exercised.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import unifideck.auth.edge_browser.env as ENV
from unifideck.auth.edge_browser.env import (
    _apply_fallbacks,
    _detect_session_env,
    _parse_proc_environ,
    _read_gamescope_env_file,
    _scan_steam_process_env,
    _seed_from_own_env,
    clean_env,
)


def test_module_imports() -> None:
    assert hasattr(ENV, "clean_env")


# ========================================================= #
# _seed_from_own_env
# ========================================================= #
def test_seed_from_own_env(monkeypatch) -> None:
    monkeypatch.setattr(
        ENV.os, "environ",
        {"DISPLAY": ":1", "IRRELEVANT": "x"})
    out: dict[str, str] = {}
    _seed_from_own_env(out)
    assert out == {"DISPLAY": ":1"}


def test_seed_from_own_env_empty(
    monkeypatch,
) -> None:
    monkeypatch.setattr(ENV.os, "environ", {})
    out: dict[str, str] = {}
    _seed_from_own_env(out)
    assert out == {}


# ========================================================= #
# _read_gamescope_env_file
# ========================================================= #
def test_read_gamescope_missing(tmp_path) -> None:
    out: dict[str, str] = {}
    _read_gamescope_env_file(str(tmp_path), out)
    assert out == {}


def test_read_gamescope_ok(tmp_path) -> None:
    f = tmp_path / "gamescope-environment"
    f.write_text(
        "DISPLAY=:5\n"
        "WAYLAND_DISPLAY=wayland-1\n"
        "# comment\n"
        "noequals\n"
        "IRRELEVANT=x\n")
    out: dict[str, str] = {}
    _read_gamescope_env_file(str(tmp_path), out)
    assert out["DISPLAY"] == ":5"
    assert out["WAYLAND_DISPLAY"] == "wayland-1"
    assert "IRRELEVANT" not in out


def test_read_gamescope_skips_present(
    tmp_path,
) -> None:
    f = tmp_path / "gamescope-environment"
    f.write_text("DISPLAY=:9\n")
    out = {"DISPLAY": ":0"}  # already present
    _read_gamescope_env_file(str(tmp_path), out)
    assert out["DISPLAY"] == ":0"  # not overwritten


def test_read_gamescope_oserror(
    tmp_path, monkeypatch,
) -> None:
    f = tmp_path / "gamescope-environment"
    f.write_text("DISPLAY=:1\n")

    def _boom(self, *a: Any, **k: Any):
        raise OSError("vanished")

    monkeypatch.setattr(Path, "open", _boom)
    out: dict[str, str] = {}
    _read_gamescope_env_file(str(tmp_path), out)
    assert out == {}  # swallowed


# ========================================================= #
# _parse_proc_environ
# ========================================================= #
def test_parse_proc_environ_missing() -> None:
    out: dict[str, str] = {}
    assert _parse_proc_environ(
        "999999", out) is False


def test_parse_proc_environ_ok(
    tmp_path, monkeypatch,
) -> None:
    envfile = tmp_path / "environ"
    envfile.write_bytes(
        b"DISPLAY=:3\x00WAYLAND_DISPLAY=wl-2\x00"
        b"NOEQ\x00IRRELEVANT=x\x00")

    real_open = Path.open

    def _fake(self, *a: Any, **k: Any):
        if "/proc/" in str(self):
            return real_open(envfile, *a, **k)
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", _fake)
    out: dict[str, str] = {}
    found = _parse_proc_environ("123", out)
    assert found is True
    assert out["DISPLAY"] == ":3"
    assert out["WAYLAND_DISPLAY"] == "wl-2"


def test_parse_proc_environ_no_display(
    tmp_path, monkeypatch,
) -> None:
    envfile = tmp_path / "environ"
    envfile.write_bytes(b"DESKTOP_SESSION=plasma\x00")

    real_open = Path.open

    def _fake(self, *a: Any, **k: Any):
        if "/proc/" in str(self):
            return real_open(envfile, *a, **k)
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", _fake)
    out: dict[str, str] = {}
    assert _parse_proc_environ("123", out) is False
    assert out["DESKTOP_SESSION"] == "plasma"


# ========================================================= #
# _scan_steam_process_env
# ========================================================= #
def test_scan_steam_finds(monkeypatch) -> None:
    class _R:
        stdout = "111\n"

    monkeypatch.setattr(
        ENV.subprocess, "run",
        lambda *a, **k: _R())
    monkeypatch.setattr(
        ENV, "_parse_proc_environ",
        lambda pid, result: True)
    out: dict[str, str] = {}
    _scan_steam_process_env(1000, out)  # returns early


def test_scan_steam_empty_pids(
    monkeypatch,
) -> None:
    class _R:
        stdout = "\n"

    monkeypatch.setattr(
        ENV.subprocess, "run",
        lambda *a, **k: _R())
    out: dict[str, str] = {}
    _scan_steam_process_env(1000, out)  # no crash


def test_scan_steam_pgrep_missing(
    monkeypatch,
) -> None:
    def _boom(*a: Any, **k: Any):
        raise FileNotFoundError("pgrep")

    monkeypatch.setattr(
        ENV.subprocess, "run", _boom)
    out: dict[str, str] = {}
    _scan_steam_process_env(1000, out)  # swallowed


# ========================================================= #
# _apply_fallbacks
# ========================================================= #
def test_apply_fallbacks_display(tmp_path) -> None:
    out: dict[str, str] = {}
    _apply_fallbacks(
        1000, str(tmp_path), str(tmp_path), out)
    assert out["DISPLAY"] == ":0"
    assert out["XDG_RUNTIME_DIR"] == str(tmp_path)


def test_apply_fallbacks_dbus(tmp_path) -> None:
    (tmp_path / "bus").write_text("x")
    out: dict[str, str] = {}
    _apply_fallbacks(
        1000, str(tmp_path), str(tmp_path), out)
    assert out["DBUS_SESSION_BUS_ADDRESS"] == \
        f"unix:path={tmp_path}/bus"


def test_apply_fallbacks_xauth_glob(
    tmp_path,
) -> None:
    (tmp_path / "xauth_abc").write_text("x")
    out: dict[str, str] = {}
    _apply_fallbacks(
        1000, str(tmp_path), str(tmp_path), out)
    assert "xauth_abc" in out["XAUTHORITY"]


def test_apply_fallbacks_xauth_home(
    tmp_path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".Xauthority").write_text("x")
    rt = tmp_path / "rt"
    rt.mkdir()
    out: dict[str, str] = {}
    _apply_fallbacks(
        1000, str(home), str(rt), out)
    assert out["XAUTHORITY"] == str(
        home / ".Xauthority")


def test_apply_fallbacks_gamescope_socket(
    tmp_path,
) -> None:
    (tmp_path / "gamescope-0").write_text("x")
    out = {
        "GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0",
        "XDG_RUNTIME_DIR": str(tmp_path),
        "DISPLAY": ":0"}
    _apply_fallbacks(
        1000, str(tmp_path), str(tmp_path), out)
    assert out["WAYLAND_DISPLAY"] == "gamescope-0"


def test_apply_fallbacks_steam_xmodifiers(
    tmp_path,
) -> None:
    out = {
        "GTK_IM_MODULE": "Steam",
        "DISPLAY": ":0"}
    _apply_fallbacks(
        1000, str(tmp_path), str(tmp_path), out)
    assert out["XMODIFIERS"] == "@im=Steam"


def test_apply_fallbacks_all_present(
    tmp_path,
) -> None:
    out = {
        "DISPLAY": ":1",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "DBUS_SESSION_BUS_ADDRESS": "x",
        "XAUTHORITY": "/x"}
    _apply_fallbacks(
        1000, str(tmp_path), str(tmp_path), out)
    assert out["DISPLAY"] == ":1"  # untouched


# ========================================================= #
# _detect_session_env
# ========================================================= #
def test_detect_session_env(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(ENV.os, "environ", {})
    monkeypatch.setattr(
        ENV, "_scan_steam_process_env",
        lambda uid, result: None)
    out = _detect_session_env(1000, str(tmp_path))
    # fallback applied DISPLAY
    assert out["DISPLAY"] == ":0"


# ========================================================= #
# clean_env
# ========================================================= #
def test_clean_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        ENV.os, "environ",
        {"LD_PRELOAD": "x", "LD_LIBRARY_PATH": "y",
         "KEEP": "z"})
    monkeypatch.setattr(
        ENV.Path, "home",
        staticmethod(lambda: tmp_path))
    monkeypatch.setattr(
        ENV, "_detect_session_env",
        lambda uid, home: {"DISPLAY": ":0"})
    env = clean_env()
    assert "LD_PRELOAD" not in env
    assert "LD_LIBRARY_PATH" not in env
    assert env["KEEP"] == "z"
    assert env["DISPLAY"] == ":0"
    assert env["SteamGameId"] == "0"
    assert env["GTK_MODULES"] == ""
