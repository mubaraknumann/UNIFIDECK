"""Tests for ``stores.gamevault.store.GameVaultStore`` — orchestration
logic between auth/installer/library-reader, with those collaborators
mocked out (their own behaviour is covered by
test_gamevault_auth/install/library.py).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.stores.gamevault.store import GameVaultStore


def _make_store(**auth_overrides) -> GameVaultStore:
    """Build a GameVaultStore with a fake bus/cache and no real config,
    then monkeypatch its internal collaborators with mocks."""
    bus = MagicMock()
    cache = MagicMock()
    store = GameVaultStore(bus, cache, plugin_dir=None, config=None)

    store._auth = MagicMock()
    store._auth.is_authenticated.return_value = auth_overrides.get("is_authenticated", True)
    store._auth.get_auth_headers = AsyncMock(
        return_value=auth_overrides.get("auth_headers", {"Authorization": "Bearer x"}),
    )
    store._auth.server_url = auth_overrides.get("server_url", "https://gv.example.com")
    store._auth.verify_ssl = auth_overrides.get("verify_ssl", True)
    store._auth.download_dir = auth_overrides.get("download_dir", None)

    store._installer = MagicMock()
    store._library_reader = MagicMock()
    return store


# ── is_available ────────────────────────────────────────────────────────
async def test_is_available_reflects_auth_state():
    store = _make_store(is_authenticated=True)
    assert await store.is_available() is True

    store2 = _make_store(is_authenticated=False)
    assert await store2.is_available() is False


# ── start_auth / complete_auth ────────────────────────────────────────
async def test_start_auth_forwards_kwargs_to_auth_module():
    store = _make_store()
    store._auth.start_auth = AsyncMock(return_value=MagicMock(success=True))

    await store.start_auth(
        server_url="https://gv.example.com",
        username="alice",
        password="secret",
        verify_ssl=False,
        download_dir="/mnt/dl",
    )

    store._auth.start_auth.assert_awaited_once_with(
        server_url="https://gv.example.com",
        username="alice",
        password="secret",
        verify_ssl=False,
        download_dir="/mnt/dl",
    )


async def test_start_auth_defaults_missing_kwargs():
    store = _make_store()
    store._auth.start_auth = AsyncMock(return_value=MagicMock(success=True))

    await store.start_auth()

    store._auth.start_auth.assert_awaited_once_with(
        server_url="", username="", password="", verify_ssl=True, download_dir=None,
    )


async def test_complete_auth_success_when_authenticated():
    store = _make_store(is_authenticated=True)
    result = await store.complete_auth()
    assert result.success is True
    assert result.action == "authenticated"


async def test_complete_auth_failure_when_not_authenticated():
    store = _make_store(is_authenticated=False)
    result = await store.complete_auth()
    assert result.success is False


# ── logout ────────────────────────────────────────────────────────────
async def test_logout_delegates_to_auth():
    store = _make_store()
    store._auth.logout = AsyncMock(return_value=MagicMock(success=True))
    result = await store.logout()
    store._auth.logout.assert_awaited_once()
    assert result.success is True


# ── get_library ───────────────────────────────────────────────────────
async def test_get_library_returns_none_when_not_authenticated():
    store = _make_store()
    store._auth.get_auth_headers = AsyncMock(return_value=None)
    assert await store.get_library() is None


async def test_get_library_returns_games_on_success():
    store = _make_store()
    fake_games = [MagicMock()]
    store._library_reader.get_library = AsyncMock(return_value=fake_games)

    result = await store.get_library()

    assert result is fake_games
    store._library_reader.get_library.assert_awaited_once()


async def test_get_library_returns_none_on_exception():
    store = _make_store()
    store._library_reader.get_library = AsyncMock(side_effect=RuntimeError("boom"))

    result = await store.get_library()

    assert result is None


# ── install_game ──────────────────────────────────────────────────────
async def test_install_game_fails_fast_without_auth():
    store = _make_store()
    store._auth.get_auth_headers = AsyncMock(return_value=None)

    result = await store.install_game("123")

    assert result.success is False
    assert result.error == "Not authenticated"
    store._installer.install_game.assert_not_called()


async def test_install_game_forwards_to_installer():
    store = _make_store()
    store._installer.install_game = AsyncMock(return_value=MagicMock(success=True))

    await store.install_game("123", base_path="/games", progress_cb=None)

    store._installer.install_game.assert_awaited_once()
    _, kwargs = store._installer.install_game.call_args
    assert kwargs["install_path"] == "/games"
    assert kwargs["server_url"] == "https://gv.example.com"


async def test_install_game_download_dir_precedence(monkeypatch):
    """kwargs['download_dir'] should win over the saved auth.download_dir."""
    store = _make_store(download_dir="/saved/dir")
    store._installer.install_game = AsyncMock(return_value=MagicMock(success=True))

    await store.install_game("123", download_dir="/override/dir")

    _, kwargs = store._installer.install_game.call_args
    assert kwargs["download_dir"] == "/override/dir"


async def test_install_game_falls_back_to_saved_download_dir():
    store = _make_store(download_dir="/saved/dir")
    store._installer.install_game = AsyncMock(return_value=MagicMock(success=True))

    await store.install_game("123")

    _, kwargs = store._installer.install_game.call_args
    assert kwargs["download_dir"] == "/saved/dir"


# ── uninstall_game / update_game / check_for_updates ───────────────────
async def test_uninstall_game_delegates_to_installer():
    store = _make_store()
    store._installer.uninstall_game = AsyncMock(return_value=MagicMock(success=True))

    await store.uninstall_game("123")

    store._installer.uninstall_game.assert_awaited_once_with("123")


async def test_update_game_reinstalls():
    store = _make_store()
    store._installer.install_game = AsyncMock(return_value=MagicMock(success=True))

    await store.update_game("123")

    store._installer.install_game.assert_awaited_once()


async def test_check_for_updates_always_empty():
    store = _make_store()
    assert await store.check_for_updates() == []


# ── get_game_size ─────────────────────────────────────────────────────
async def test_get_game_size_returns_none_without_auth():
    store = _make_store()
    store._auth.get_auth_headers = AsyncMock(return_value=None)
    assert await store.get_game_size("123") is None


async def test_get_game_size_forwards_to_installer():
    store = _make_store()
    store._installer.get_game_size = AsyncMock(return_value=1234)

    result = await store.get_game_size("123")

    assert result == 1234
    store._installer.get_game_size.assert_awaited_once()


# ── backward-compat helpers ───────────────────────────────────────────
def test_get_install_info_delegates_to_installer():
    store = _make_store()
    store._installer.get_install_info.return_value = {"title": "T"}
    assert store._get_install_info("123") == {"title": "T"}


async def test_get_installed_delegates_to_installer():
    store = _make_store()
    store._installer.get_installed.return_value = {"1": {"title": "A"}}
    result = await store.get_installed()
    assert result == {"1": {"title": "A"}}


# ── store_info sanity ─────────────────────────────────────────────────
def test_store_info_declares_no_wine_and_manual_auth():
    assert GameVaultStore.store_info.name == "gamevault"
    assert GameVaultStore.store_info.uses_wine is False
    assert GameVaultStore.store_info.auth_method == "manual"
    assert GameVaultStore.store_info.supports_install is True
    assert GameVaultStore.store_info.supports_cloud_saves is False
