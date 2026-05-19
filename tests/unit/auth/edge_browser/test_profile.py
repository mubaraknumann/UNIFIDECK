"""Deep executable tests — auth/edge_browser/profile.py.

Source : py_modules/unifideck/auth/edge_browser/profile.py
Fiche  : OP (auth/)   Critical — coverage floor 95%.

EdgeProfileManager: legacy migration, stale-singleton
cleanup, xbox-cookie inspection (real sqlite DBs) and full
profile wipe. All paths are tmp_path-scoped.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from unifideck.auth.edge_browser.profile import (
    EdgeProfileManager,
)


def _mgr(tmp_path) -> EdgeProfileManager:
    return EdgeProfileManager(
        profile_dir=str(tmp_path / "edge-auth"),
        log_file=str(tmp_path / "edge-auth.log"),
        legacy_profile_dir=str(
            tmp_path / "chromium-auth"),
        legacy_log_file=str(
            tmp_path / "chromium-auth.log"),
        cookie_domain_patterns=(
            "%xbox.com%", "%live.com%"))


@pytest.fixture()
def mgr(tmp_path) -> EdgeProfileManager:
    return _mgr(tmp_path)


def _make_cookie_db(path: Path,
                    hosts: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE cookies (host_key TEXT)")
    for h in hosts:
        conn.execute(
            "INSERT INTO cookies VALUES (?)", (h,))
    conn.commit()
    conn.close()


def test_module_imports() -> None:
    import unifideck.auth.edge_browser.profile as mod
    assert mod.EdgeProfileManager is \
        EdgeProfileManager


# ========================================================= #
# migrate_legacy_profile
# ========================================================= #
def test_migrate_no_legacy(mgr) -> None:
    mgr.migrate_legacy_profile()  # nothing to do, no raise


def test_migrate_both_exist(tmp_path) -> None:
    m = _mgr(tmp_path)
    Path(m.legacy_profile_dir).mkdir(parents=True)
    Path(m.profile_dir).mkdir(parents=True)
    m.migrate_legacy_profile()
    # legacy left intact
    assert Path(m.legacy_profile_dir).is_dir()


def test_migrate_success(tmp_path) -> None:
    m = _mgr(tmp_path)
    legacy = Path(m.legacy_profile_dir)
    legacy.mkdir(parents=True)
    (legacy / "Cookies").write_text("data")
    m.migrate_legacy_profile()
    assert Path(m.profile_dir).is_dir()
    assert not legacy.exists()


def test_migrate_moves_log_too(tmp_path) -> None:
    m = _mgr(tmp_path)
    Path(m.legacy_profile_dir).mkdir(parents=True)
    Path(m.legacy_log_file).write_text("log")
    m.migrate_legacy_profile()
    assert Path(m.log_file).is_file()


def test_migrate_move_oserror(
    tmp_path, monkeypatch,
) -> None:
    m = _mgr(tmp_path)
    Path(m.legacy_profile_dir).mkdir(parents=True)
    import unifideck.auth.edge_browser.profile as mod

    def _boom(s, d):
        raise OSError("cross device")

    monkeypatch.setattr(mod.shutil, "move", _boom)
    m.migrate_legacy_profile()  # swallowed


def test_migrate_log_move_oserror(
    tmp_path, monkeypatch,
) -> None:
    m = _mgr(tmp_path)
    Path(m.legacy_profile_dir).mkdir(parents=True)
    Path(m.legacy_log_file).write_text("log")
    import unifideck.auth.edge_browser.profile as mod

    real_move = mod.shutil.move
    calls = {"n": 0}

    def _move(s, d):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_move(s, d)  # profile ok
        raise OSError("log move failed")

    monkeypatch.setattr(mod.shutil, "move", _move)
    m.migrate_legacy_profile()  # log failure swallowed


# ========================================================= #
# _singleton_paths / _has_stale_singleton_socket
# ========================================================= #
def test_singleton_paths(mgr) -> None:
    paths = mgr._singleton_paths()
    assert len(paths) == 3
    assert any("SingletonLock" in p for p in paths)


def test_has_stale_socket_not_symlink(
    tmp_path,
) -> None:
    m = _mgr(tmp_path)
    Path(m.profile_dir).mkdir(parents=True)
    assert m._has_stale_singleton_socket() is False


def test_has_stale_socket_broken(tmp_path) -> None:
    m = _mgr(tmp_path)
    Path(m.profile_dir).mkdir(parents=True)
    sock = Path(m.profile_dir) / "SingletonSocket"
    os.symlink("/no/such/target", sock)
    assert m._has_stale_singleton_socket() is True


def test_has_stale_socket_valid(tmp_path) -> None:
    m = _mgr(tmp_path)
    Path(m.profile_dir).mkdir(parents=True)
    real = tmp_path / "realtarget"
    real.write_text("x")
    sock = Path(m.profile_dir) / "SingletonSocket"
    os.symlink(real, sock)
    assert m._has_stale_singleton_socket() is False


# ========================================================= #
# cleanup_stale_state
# ========================================================= #
def test_cleanup_stale_state_nothing(
    tmp_path,
) -> None:
    m = _mgr(tmp_path)
    Path(m.profile_dir).mkdir(parents=True)
    m.cleanup_stale_state()  # no stale socket, no-op


def test_cleanup_stale_state_removes(
    tmp_path,
) -> None:
    m = _mgr(tmp_path)
    pdir = Path(m.profile_dir)
    pdir.mkdir(parents=True)
    os.symlink("/no/such", pdir / "SingletonSocket")
    (pdir / "SingletonLock").write_text("x")
    (pdir / "SingletonCookie").write_text("x")
    m.cleanup_stale_state()
    assert not (pdir / "SingletonLock").exists()


def test_cleanup_stale_state_unlink_oserror(
    tmp_path, monkeypatch,
) -> None:
    m = _mgr(tmp_path)
    pdir = Path(m.profile_dir)
    pdir.mkdir(parents=True)
    os.symlink("/no/such", pdir / "SingletonSocket")
    (pdir / "SingletonLock").write_text("x")

    real_unlink = Path.unlink

    def _boom(self, *a, **k):
        if self.name == "SingletonLock":
            raise OSError("perm")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", _boom)
    m.cleanup_stale_state()  # OSError logged, swallowed


# ========================================================= #
# has_xbox_session
# ========================================================= #
def test_has_xbox_session_no_db(mgr) -> None:
    # profile doesn't exist -> True (no logout detected)
    assert mgr.has_xbox_session() is True


def test_has_xbox_session_with_xbox_cookie(
    tmp_path,
) -> None:
    m = _mgr(tmp_path)
    db = Path(m.profile_dir) / "Default" / "Cookies"
    _make_cookie_db(
        db, ["login.xbox.com", "other.com"])
    assert m.has_xbox_session() is True


def test_has_xbox_session_no_xbox_cookie(
    tmp_path,
) -> None:
    m = _mgr(tmp_path)
    db = Path(m.profile_dir) / "Default" / "Cookies"
    _make_cookie_db(db, ["unrelated.com"])
    assert m.has_xbox_session() is False


def test_has_xbox_session_corrupt_db(
    tmp_path,
) -> None:
    m = _mgr(tmp_path)
    db = Path(m.profile_dir) / "Default" / "Cookies"
    db.parent.mkdir(parents=True)
    db.write_text("not a sqlite db")
    # error -> assume logged in (True)
    assert m.has_xbox_session() is True


# ========================================================= #
# clear_cookies
# ========================================================= #
def test_clear_cookies_no_db(mgr) -> None:
    mgr.clear_cookies()  # no db, no raise


def test_clear_cookies_removes(tmp_path) -> None:
    m = _mgr(tmp_path)
    db = Path(m.profile_dir) / "Default" / "Cookies"
    _make_cookie_db(
        db, ["login.xbox.com", "auth.live.com",
              "keep.example.com"])
    m.clear_cookies()
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT host_key FROM cookies").fetchall()
    conn.close()
    hosts = {r[0] for r in rows}
    assert "keep.example.com" in hosts
    assert "login.xbox.com" not in hosts


def test_clear_cookies_corrupt_db(
    tmp_path,
) -> None:
    m = _mgr(tmp_path)
    db = Path(m.profile_dir) / "Default" / "Cookies"
    db.parent.mkdir(parents=True)
    db.write_text("garbage")
    m.clear_cookies()  # error swallowed


# ========================================================= #
# clear_profile_data
# ========================================================= #
def test_clear_profile_data_removes(tmp_path) -> None:
    m = _mgr(tmp_path)
    pdir = Path(m.profile_dir)
    pdir.mkdir(parents=True)
    (pdir / "f").write_text("x")
    Path(m.log_file).write_text("log")
    m.clear_profile_data()
    assert not pdir.exists()
    assert not Path(m.log_file).exists()


def test_clear_profile_data_nothing(mgr) -> None:
    mgr.clear_profile_data()  # nothing exists, no raise


def test_clear_profile_data_rmtree_error(
    tmp_path, monkeypatch,
) -> None:
    m = _mgr(tmp_path)
    Path(m.profile_dir).mkdir(parents=True)
    import unifideck.auth.edge_browser.profile as mod

    def _boom(p):
        raise OSError("busy")

    monkeypatch.setattr(mod.shutil, "rmtree", _boom)
    m.clear_profile_data()  # warning logged, swallowed
