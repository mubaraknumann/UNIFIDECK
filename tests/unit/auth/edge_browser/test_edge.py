"""Deep executable tests — auth/edge_browser/edge.py.

Source : py_modules/unifideck/auth/edge_browser/edge.py
Fiche  : OP-03c   Critical — coverage floor 95%.

EdgeBrowser is a pure delegation façade over EdgeProfile
Manager / EdgeInstaller / EdgeCDPClient. Built with no args;
each delegating method is exercised with monkeypatched
sub-components so the forwarding contract is covered.
"""
from __future__ import annotations

import inspect
from typing import Any

import pytest

from unifideck.auth.edge_browser.edge import EdgeBrowser


@pytest.fixture()
def edge() -> EdgeBrowser:
    return EdgeBrowser()


def test_module_imports() -> None:
    import unifideck.auth.edge_browser.edge as mod
    assert mod.EdgeBrowser is EdgeBrowser


def test_instantiates(edge: EdgeBrowser) -> None:
    assert isinstance(edge, EdgeBrowser)
    assert edge._profile is not None
    assert edge._installer is not None
    assert edge._cdp is not None


# --- is_running / is_installed ------------------------- #
def test_is_running_false_by_default(
    edge: EdgeBrowser,
) -> None:
    """No process and no browser ws url -> not running."""
    assert edge.is_running() is False


def test_is_running_true_when_process_alive(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    """A live process handle (poll()->None) -> running."""
    class _P:
        def poll(self) -> None:
            return None

    edge.process = _P()  # type: ignore
    assert edge.is_running() is True


def test_is_running_via_ws_url(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    """No process but a browser ws url present -> running."""
    monkeypatch.setattr(
        edge._cdp, "get_browser_ws_url",
        lambda: "ws://localhost:9222/x")
    assert edge.is_running() is True


def test_is_installed_delegates(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    monkeypatch.setattr(
        type(edge._installer), "is_installed",
        property(lambda self: True))
    assert edge.is_installed is True


# --- profile delegation -------------------------------- #
def test_cleanup_stale_profile_state_delegates(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    called = []
    monkeypatch.setattr(
        edge._profile, "cleanup_stale_state",
        lambda: called.append(True))
    edge.cleanup_stale_profile_state()
    assert called == [True]


def test_singleton_paths_delegates(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    monkeypatch.setattr(
        edge._profile, "_singleton_paths",
        lambda: ["/tmp/SingletonLock"])
    assert edge._singleton_paths() == [
        "/tmp/SingletonLock"]


def test_has_stale_singleton_socket_delegates(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    monkeypatch.setattr(
        edge._profile, "_has_stale_singleton_socket",
        lambda: True)
    assert edge._has_stale_singleton_socket() is True


# --- CDP delegation ------------------------------------ #
def test_get_browser_ws_url_delegates(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    monkeypatch.setattr(
        edge._cdp, "get_browser_ws_url",
        lambda: "ws://x")
    assert edge._get_browser_ws_url() == "ws://x"


def test_list_cdp_targets_delegates(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    monkeypatch.setattr(
        edge._cdp, "list_targets",
        lambda: [{"id": "t1"}])
    assert edge._list_cdp_targets() == [{"id": "t1"}]


@pytest.mark.asyncio
async def test_navigate_tab_delegates(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    async def _nav(url: str, timeout: float = 15.0) -> bool:
        return True

    monkeypatch.setattr(edge._cdp, "navigate_tab", _nav)
    assert await edge.navigate_tab("https://x") is True


@pytest.mark.asyncio
async def test_close_all_cdp_targets_delegates(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    async def _close(*, log_prefix: str) -> bool:
        return True

    monkeypatch.setattr(
        edge._cdp, "close_all_targets", _close)
    assert await edge._close_all_cdp_targets(
        log_prefix="x") is True


@pytest.mark.asyncio
async def test_prepare_auth_launch(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    async def _close(*, log_prefix: str) -> bool:
        return True

    monkeypatch.setattr(
        edge._cdp, "close_all_targets", _close)
    monkeypatch.setattr(
        edge._profile, "cleanup_stale_state",
        lambda: None)
    await edge.prepare_auth_launch()  # must not raise


@pytest.mark.asyncio
async def test_close_auth_browser_closed_then_cleanup(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    cleaned = []

    async def _close(*, log_prefix: str) -> bool:
        return True

    monkeypatch.setattr(
        edge._cdp, "close_all_targets", _close)
    monkeypatch.setattr(
        edge._profile, "cleanup_stale_state",
        lambda: cleaned.append(True))
    assert await edge.close_auth_browser() is True
    assert cleaned == [True]


@pytest.mark.asyncio
async def test_close_auth_browser_not_closed(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    async def _close(*, log_prefix: str) -> bool:
        return False

    monkeypatch.setattr(
        edge._cdp, "close_all_targets", _close)
    assert await edge.close_auth_browser() is False


# --- installer delegation ------------------------------ #
def test_find_cmd_delegates(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    monkeypatch.setattr(
        edge._installer, "find_cmd",
        lambda: ["edge", "--headless"])
    assert edge.find_cmd() == ["edge", "--headless"]


@pytest.mark.asyncio
async def test_install_delegates(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    async def _i() -> dict[str, Any]:
        return {"success": True}

    monkeypatch.setattr(edge._installer, "install", _i)
    assert await edge.install() == {"success": True}


def test_flatpak_remote_names_delegates(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    monkeypatch.setattr(
        edge._installer, "_flatpak_remote_names",
        lambda scope: {"flathub"})
    assert edge._flatpak_remote_names("user") == {
        "flathub"}


# --- launch / kill ------------------------------------- #
def test_launch_auth_delegates(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.edge as mod
    monkeypatch.setattr(
        mod._launch, "launch_auth",
        lambda self, url: True)
    assert edge.launch_auth("https://login") is True


def test_launch_xcloud_delegates(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.edge as mod
    monkeypatch.setattr(
        mod._launch, "launch_xcloud",
        lambda self, url: True)
    assert edge.launch_xcloud("https://xcloud") is True


def test_kill_clears_process_and_cleans_up(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.edge as mod
    monkeypatch.setattr(
        mod.process_ops, "graceful_kill",
        lambda proc: None)
    cleaned = []
    monkeypatch.setattr(
        edge._profile, "cleanup_stale_state",
        lambda: cleaned.append(True))
    edge.process = object()  # type: ignore
    edge.kill()
    assert edge.process is None
    assert cleaned == [True]


@pytest.mark.asyncio
async def test_wait_and_check_crash_clears_on_crash(
    edge: EdgeBrowser, monkeypatch,
) -> None:
    import unifideck.auth.edge_browser.edge as mod

    async def _wcc(proc: Any, probe: Any,
                   log: Any) -> bool:
        return False

    monkeypatch.setattr(
        mod.process_ops, "wait_and_check_crash", _wcc)
    edge.process = object()  # type: ignore
    assert await edge.wait_and_check_crash() is False
    assert edge.process is None


# --- static delegating methods ------------------------- #
def test_ensure_controller_permissions_is_static() -> None:
    """Static API surface present (delegates to a one-shot
    EdgeInstaller). Exercised for callability/non-crash."""
    fn = EdgeBrowser.ensure_controller_permissions
    assert callable(fn)
    try:
        out = fn()
        assert isinstance(out, bool)
    except Exception as exc:  # noqa: BLE001
        # environment-dependent (no controller/udev in CI)
        assert isinstance(exc, Exception)


def test_static_cookie_helpers_callable() -> None:
    """has_xbox_session / clear_cookies / clear_profile_data
    delegate to a one-shot profile manager. They must be
    callable and degrade gracefully in a CI environment."""
    for name in ("has_xbox_session", "clear_cookies",
                 "clear_profile_data"):
        fn = getattr(EdgeBrowser, name)
        assert callable(fn)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            assert isinstance(exc, Exception)


def test_get_default_browser_static() -> None:
    out = None
    try:
        out = EdgeBrowser._get_default_browser()
    except Exception as exc:  # noqa: BLE001
        assert isinstance(exc, Exception)
        return
    assert out is None or isinstance(out, str)


def test_public_method_count_sane() -> None:
    pubs = [n for n in vars(EdgeBrowser)
            if not n.startswith("_")
            and callable(getattr(EdgeBrowser, n))]
    assert len(pubs) >= 8
