"""Deep executable tests — auth/edge_browser/installer.py.

Source : py_modules/unifideck/auth/edge_browser/installer.py
Fiche  : OP (auth/)   Critical — coverage floor 95%.

EdgeInstaller: flatpak Edge install + controller-permission
override + default-browser snapshot/restore. shutil.which,
subprocess and the detection helpers are stubbed; no real
flatpak is invoked.
"""
from __future__ import annotations

import subprocess
from typing import Any

import pytest

from unifideck.auth.edge_browser.installer import (
    EdgeInstaller,
)


def _inst() -> EdgeInstaller:
    return EdgeInstaller(clean_env_fn=lambda: {"X": "1"})


@pytest.fixture()
def inst() -> EdgeInstaller:
    return _inst()


class _CP:
    def __init__(self, rc: int = 0,
                 out: bytes = b"",
                 err: bytes = b"",
                 text_out: str = "") -> None:
        self.returncode = rc
        self.stdout = text_out or out
        self.stderr = err


def test_module_imports() -> None:
    import unifideck.auth.edge_browser.installer as mod
    assert mod.EdgeInstaller is EdgeInstaller


# ========================================================= #
# ensure_controller_permissions
# ========================================================= #
def test_ensure_perms_no_flatpak(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    monkeypatch.setattr(
        mod.shutil, "which", lambda n: None)
    assert inst.ensure_controller_permissions() is False


def test_ensure_perms_already_present(
    inst, tmp_path, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    monkeypatch.setattr(
        mod.shutil, "which", lambda n: "/usr/bin/flatpak")
    ov = tmp_path / "com.microsoft.Edge"
    ov.write_text("[Context]\nfilesystems=/run/udev:ro;")

    real_path = mod.Path

    def _fake(arg: Any = "", *a: Any, **k: Any):
        if "overrides" in str(arg):
            return real_path(str(ov))
        return real_path(arg, *a, **k)

    monkeypatch.setattr(mod, "Path", _fake)
    assert inst.ensure_controller_permissions() is True


def test_ensure_perms_applies_override(
    inst, tmp_path, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    monkeypatch.setattr(
        mod.shutil, "which", lambda n: "/usr/bin/flatpak")
    real_path = mod.Path

    def _fake(arg: Any = "", *a: Any, **k: Any):
        if "overrides" in str(arg):
            return real_path(
                str(tmp_path / "nonexistent"))
        return real_path(arg, *a, **k)

    monkeypatch.setattr(mod, "Path", _fake)
    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *a, **k: _CP(rc=0))
    assert inst.ensure_controller_permissions() is True


def test_ensure_perms_override_fails(
    inst, tmp_path, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    monkeypatch.setattr(
        mod.shutil, "which", lambda n: "/usr/bin/flatpak")
    real_path = mod.Path

    def _fake(arg: Any = "", *a: Any, **k: Any):
        if "overrides" in str(arg):
            return real_path(
                str(tmp_path / "none"))
        return real_path(arg, *a, **k)

    monkeypatch.setattr(mod, "Path", _fake)
    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *a, **k: _CP(rc=1, err=b"denied"))
    assert inst.ensure_controller_permissions() is False


def test_ensure_perms_override_exception(
    inst, tmp_path, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    monkeypatch.setattr(
        mod.shutil, "which", lambda n: "/usr/bin/flatpak")
    real_path = mod.Path

    def _fake(arg: Any = "", *a: Any, **k: Any):
        if "overrides" in str(arg):
            return real_path(str(tmp_path / "none"))
        return real_path(arg, *a, **k)

    monkeypatch.setattr(mod, "Path", _fake)

    def _boom(*a, **k):
        raise OSError("subprocess gone")

    monkeypatch.setattr(mod.subprocess, "run", _boom)
    assert inst.ensure_controller_permissions() is False


# ========================================================= #
# detection thin wrappers
# ========================================================= #
def test_flatpak_remote_names_delegates(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    monkeypatch.setattr(
        mod, "flatpak_remote_names",
        lambda fn, scope: {"flathub"})
    assert inst._flatpak_remote_names("--user") == \
        {"flathub"}


def test_find_cmd_delegates(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    monkeypatch.setattr(
        mod, "find_edge_cmd",
        lambda fn: ["flatpak", "run", "edge"])
    assert inst.find_cmd() == [
        "flatpak", "run", "edge"]


def test_is_installed_delegates(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    monkeypatch.setattr(
        mod, "is_edge_installed", lambda fn: True)
    assert inst.is_installed is True


# ========================================================= #
# _ensure_user_flathub_remote
# ========================================================= #
@pytest.mark.asyncio
async def test_flathub_remote_already_present(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    monkeypatch.setattr(
        mod, "flatpak_remote_names",
        lambda fn, scope: {"flathub"})
    assert await inst._ensure_user_flathub_remote() \
        is True


@pytest.mark.asyncio
async def test_flathub_remote_added(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    calls = {"n": 0}

    def _remotes(fn, scope):
        calls["n"] += 1
        # absent first, present after add
        return set() if calls["n"] == 1 else {"flathub"}

    monkeypatch.setattr(
        mod, "flatpak_remote_names", _remotes)
    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *a, **k: _CP(rc=0))
    assert await inst._ensure_user_flathub_remote() \
        is True


@pytest.mark.asyncio
async def test_flathub_remote_add_fails(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    monkeypatch.setattr(
        mod, "flatpak_remote_names",
        lambda fn, scope: set())
    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *a, **k: _CP(rc=1, err=b"net error"))
    assert await inst._ensure_user_flathub_remote() \
        is False


@pytest.mark.asyncio
async def test_flathub_remote_add_exception(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    monkeypatch.setattr(
        mod, "flatpak_remote_names",
        lambda fn, scope: set())

    def _boom(*a, **k):
        raise OSError("flatpak missing")

    monkeypatch.setattr(mod.subprocess, "run", _boom)
    assert await inst._ensure_user_flathub_remote() \
        is False


# ========================================================= #
# _get_default_browser / _restore_default_browser
# ========================================================= #
def test_get_default_browser_ok(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *a, **k: _CP(
            rc=0, text_out="firefox.desktop\n"))
    assert inst._get_default_browser() == \
        "firefox.desktop"


def test_get_default_browser_error(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    def _boom(*a, **k):
        raise OSError("xdg-settings missing")

    monkeypatch.setattr(mod.subprocess, "run", _boom)
    assert inst._get_default_browser() is None


def test_restore_default_browser_none(
    inst,
) -> None:
    inst._restore_default_browser(None)  # no-op


def test_restore_default_browser_changed(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    calls = []

    def _run(cmd, *a, **k):
        if "get" in cmd:
            return _CP(rc=0, text_out="edge.desktop\n")
        calls.append(cmd)
        return _CP(rc=0)

    monkeypatch.setattr(mod.subprocess, "run", _run)
    inst._restore_default_browser("firefox.desktop")
    assert calls  # a 'set' was issued


def test_restore_default_browser_unchanged(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    def _run(cmd, *a, **k):
        return _CP(rc=0, text_out="firefox.desktop\n")

    monkeypatch.setattr(mod.subprocess, "run", _run)
    # current == original -> no set call, no raise
    inst._restore_default_browser("firefox.desktop")


def test_restore_default_browser_exception(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    def _boom(*a, **k):
        raise OSError("gone")

    monkeypatch.setattr(mod.subprocess, "run", _boom)
    inst._restore_default_browser(
        "firefox.desktop")  # swallowed


# ========================================================= #
# install (full orchestration)
# ========================================================= #
@pytest.mark.asyncio
async def test_install_no_flatpak(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    monkeypatch.setattr(
        mod.shutil, "which", lambda n: None)
    res = await inst.install()
    assert res["success"] is False
    assert res["error"] == "microsoft.flatpakNotFound"


@pytest.mark.asyncio
async def test_install_already_installed(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    monkeypatch.setattr(
        mod.shutil, "which", lambda n: "/bin/flatpak")
    monkeypatch.setattr(
        mod, "is_edge_installed", lambda fn: True)
    res = await inst.install()
    assert res["success"] is True
    assert "AlreadyInstalled" in res["message"]


@pytest.mark.asyncio
async def test_install_flathub_remote_fails(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    monkeypatch.setattr(
        mod.shutil, "which", lambda n: "/bin/flatpak")
    monkeypatch.setattr(
        mod, "is_edge_installed", lambda fn: False)

    async def _no_remote() -> bool:
        return False

    monkeypatch.setattr(
        inst, "_ensure_user_flathub_remote", _no_remote)
    res = await inst.install()
    assert res["success"] is False
    assert res["error"] == \
        "microsoft.browserInstallFailed"


@pytest.mark.asyncio
async def test_install_success(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    monkeypatch.setattr(
        mod.shutil, "which", lambda n: "/bin/flatpak")
    monkeypatch.setattr(
        mod, "is_edge_installed", lambda fn: False)

    async def _remote() -> bool:
        return True

    async def _flatpak_install():
        return _CP(rc=0)

    async def _wait() -> None:
        return None

    monkeypatch.setattr(
        inst, "_ensure_user_flathub_remote", _remote)
    monkeypatch.setattr(
        inst, "_run_flatpak_install", _flatpak_install)
    monkeypatch.setattr(
        inst, "_wait_for_edge_ready", _wait)
    monkeypatch.setattr(
        inst, "_get_default_browser", lambda: "ff")
    monkeypatch.setattr(
        inst, "_restore_default_browser",
        lambda o: None)
    monkeypatch.setattr(
        inst, "ensure_controller_permissions",
        lambda: True)
    res = await inst.install()
    assert res["success"] is True
    assert "Installed" in res["message"]


@pytest.mark.asyncio
async def test_install_flatpak_nonzero(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    monkeypatch.setattr(
        mod.shutil, "which", lambda n: "/bin/flatpak")
    monkeypatch.setattr(
        mod, "is_edge_installed", lambda fn: False)

    async def _remote() -> bool:
        return True

    async def _flatpak_install():
        return _CP(rc=1, err=b"install error")

    monkeypatch.setattr(
        inst, "_ensure_user_flathub_remote", _remote)
    monkeypatch.setattr(
        inst, "_run_flatpak_install", _flatpak_install)
    monkeypatch.setattr(
        inst, "_get_default_browser", lambda: None)
    res = await inst.install()
    assert res["success"] is False
    assert res["error"] == \
        "microsoft.browserInstallFailed"


@pytest.mark.asyncio
async def test_install_timeout(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    monkeypatch.setattr(
        mod.shutil, "which", lambda n: "/bin/flatpak")
    monkeypatch.setattr(
        mod, "is_edge_installed", lambda fn: False)

    async def _remote() -> bool:
        return True

    async def _flatpak_install():
        raise subprocess.TimeoutExpired("flatpak", 300)

    monkeypatch.setattr(
        inst, "_ensure_user_flathub_remote", _remote)
    monkeypatch.setattr(
        inst, "_run_flatpak_install", _flatpak_install)
    monkeypatch.setattr(
        inst, "_get_default_browser", lambda: None)
    res = await inst.install()
    assert res["success"] is False
    assert res["error"] == "microsoft.edgeInstallTimeout"


@pytest.mark.asyncio
async def test_install_generic_exception(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    monkeypatch.setattr(
        mod.shutil, "which", lambda n: "/bin/flatpak")
    monkeypatch.setattr(
        mod, "is_edge_installed", lambda fn: False)

    async def _remote() -> bool:
        return True

    async def _flatpak_install():
        raise RuntimeError("unexpected")

    monkeypatch.setattr(
        inst, "_ensure_user_flathub_remote", _remote)
    monkeypatch.setattr(
        inst, "_run_flatpak_install", _flatpak_install)
    monkeypatch.setattr(
        inst, "_get_default_browser", lambda: None)
    res = await inst.install()
    assert res["success"] is False
    assert res["error"] == \
        "microsoft.browserInstallFailed"


# ========================================================= #
# _wait_for_edge_ready
# ========================================================= #
@pytest.mark.asyncio
async def test_wait_for_edge_ready_found(
    inst, monkeypatch,
) -> None:
    monkeypatch.setattr(
        inst, "find_cmd",
        lambda: ["flatpak", "run", "edge"])
    await inst._wait_for_edge_ready()  # returns at once


@pytest.mark.asyncio
async def test_wait_for_edge_ready_polls(
    inst, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.installer as mod

    state = {"n": 0}

    def _find():
        state["n"] += 1
        return ["edge"] if state["n"] >= 3 else None

    async def _fast_sleep(_s):
        return None

    monkeypatch.setattr(inst, "find_cmd", _find)
    monkeypatch.setattr(
        mod.asyncio, "sleep", _fast_sleep)
    await inst._wait_for_edge_ready()
    assert state["n"] >= 3
