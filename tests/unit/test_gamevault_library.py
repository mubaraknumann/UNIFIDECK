"""Tests for ``stores.gamevault.library`` — title/cover extraction and
raw-API-item-to-``Game`` mapping. Pagination itself is network-bound and
left uncovered here (would need an aiohttp test server); this focuses on
the pure, easily-broken parsing helpers.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from unifideck.stores.gamevault.library import (
    GameVaultLibraryReader,
    _parse_title_from_filename,
)


def _reader() -> GameVaultLibraryReader:
    return GameVaultLibraryReader(installer=MagicMock())


# ── _parse_title_from_filename ───────────────────────────────────────
def test_parse_title_strips_extension_and_year():
    assert _parse_title_from_filename("Half-Life 2 (2004).zip") == "Half Life 2"


def test_parse_title_no_year_suffix():
    assert _parse_title_from_filename("Portal.exe") == "Portal"


def test_parse_title_underscores_and_dashes_become_spaces():
    assert _parse_title_from_filename("The_Witcher-3_Wild-Hunt.rar") == "The Witcher 3 Wild Hunt"


def test_parse_title_with_directory_prefix():
    assert _parse_title_from_filename("/mnt/games/Doom Eternal (2020).7z") == "Doom Eternal"


def test_parse_title_collapses_multiple_spaces():
    assert _parse_title_from_filename("Game   Title.zip") == "Game Title"


def test_parse_title_bracket_year_variant():
    assert _parse_title_from_filename("Cyberpunk 2077 [2020].zip") == "Cyberpunk 2077"


def test_parse_title_falls_back_to_filename_when_result_empty():
    # An input that becomes empty after stripping should fall back to the
    # original file_path rather than returning "".
    assert _parse_title_from_filename("(2020).zip") == "(2020).zip"


# ── _extract_title ────────────────────────────────────────────────────
def test_extract_title_prefers_metadata_title():
    reader = _reader()
    item = {"metadata": {"title": "Explicit Title"}, "file_path": "ignored.zip"}
    assert reader._extract_title(item) == "Explicit Title"


def test_extract_title_falls_back_to_metadata_name():
    reader = _reader()
    item = {"metadata": {"name": "Named Title"}}
    assert reader._extract_title(item) == "Named Title"


def test_extract_title_falls_back_to_file_path_parsing():
    reader = _reader()
    item = {"file_path": "My Game (2021).zip"}
    assert reader._extract_title(item) == "My Game"


def test_extract_title_falls_back_to_id_placeholder():
    reader = _reader()
    item = {"id": 42}
    assert reader._extract_title(item) == "GameVault Game #42"


# ── _extract_cover_url ───────────────────────────────────────────────
def test_extract_cover_url_top_level_field():
    reader = _reader()
    item = {"cover_image": "https://example.com/cover.jpg"}
    assert reader._extract_cover_url(item) == "https://example.com/cover.jpg"


def test_extract_cover_url_thumbnail_field():
    reader = _reader()
    item = {"thumbnail": "https://example.com/thumb.jpg"}
    assert reader._extract_cover_url(item) == "https://example.com/thumb.jpg"


def test_extract_cover_url_structured_boxart():
    reader = _reader()
    item = {"boxart": {"url": "https://example.com/box.jpg"}}
    assert reader._extract_cover_url(item) == "https://example.com/box.jpg"


def test_extract_cover_url_none_when_nothing_present():
    reader = _reader()
    assert reader._extract_cover_url({}) is None


# ── _map_to_game ──────────────────────────────────────────────────────
def test_map_to_game_builds_expected_record():
    reader = _reader()
    item = {
        "id": 5,
        "metadata": {"title": "My Game"},
        "cover_image": "https://x/cover.jpg",
        "file_path": "/games/mygame.zip",
        "release_date": "2020-01-01",
        "early_access": True,
    }
    game = reader._map_to_game(item)
    assert game is not None
    assert game.store == "gamevault"
    assert game.store_game_id == "5"
    assert game.title == "My Game"
    assert game.icon_url == "https://x/cover.jpg"
    assert game.installed is False
    assert game.metadata["early_access"] is True


def test_map_to_game_missing_id_returns_none():
    reader = _reader()
    assert reader._map_to_game({"metadata": {"title": "No ID"}}) is None


def test_map_to_game_malformed_item_returns_none_not_raises():
    reader = _reader()
    # metadata is a non-dict, exercising the try/except path
    item = {"id": 1, "metadata": None, "cover_image": None}
    game = reader._map_to_game(item)
    # Should not raise; result may be a valid Game with fallback title.
    assert game is None or game.store_game_id == "1"


# ── get_library() install-state overlay ───────────────────────────────
async def test_get_library_marks_installed_games():
    installer = MagicMock()
    installer.get_install_info.return_value = {
        "install_path": "/games/mygame",
        "exe_path": "/games/mygame/Game.exe",
    }
    reader = GameVaultLibraryReader(installer=installer)

    async def _fake_fetch(server_url, auth_headers, verify_ssl):
        return [{"id": 1, "metadata": {"title": "My Game"}}]

    reader._fetch_all_pages = _fake_fetch  # type: ignore[method-assign]

    games = await reader.get_library(
        server_url="https://gv.example.com",
        auth_headers={},
        verify_ssl=True,
    )

    assert len(games) == 1
    assert games[0].installed is True
    assert games[0].install_path == "/games/mygame"
    assert games[0].exe_path == "/games/mygame/Game.exe"


async def test_get_library_uninstalled_games_stay_uninstalled():
    installer = MagicMock()
    installer.get_install_info.return_value = None
    reader = GameVaultLibraryReader(installer=installer)

    async def _fake_fetch(server_url, auth_headers, verify_ssl):
        return [{"id": 2, "metadata": {"title": "Not Installed"}}]

    reader._fetch_all_pages = _fake_fetch  # type: ignore[method-assign]

    games = await reader.get_library(
        server_url="https://gv.example.com",
        auth_headers={},
        verify_ssl=True,
    )

    assert games[0].installed is False
