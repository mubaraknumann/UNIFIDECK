"""Tests for ``stores.gamevault.install`` — archive detection, exe finding,
and install-marker persistence.

No dedicated GameVault tests existed before this file, despite 1139 lines
of store code across install.py/library.py/auth.py/store.py. Priority here
is ``_find_executable`` — its lack of "trainer"/"cheat" in ``_UTIL_KEYWORDS``
plus size-favouring scoring is what silently picked a bundled trainer.exe
over the real game exe on a live install (Tempest Rising / GameVault),
making Unifideck launch the trainer instead of the game.
"""
from __future__ import annotations

from unifideck.stores.gamevault.archive import _detect_format
from unifideck.stores.gamevault.install import (
    GameVaultInstaller,
    _find_executable,
    _load_install_info,
    _parse_filename_from_cd,
    _remove_install_info,
    _save_install_info,
)


# ── _detect_format ──────────────────────────────────────────────────
def test_detect_format_zip(tmp_path):
    p = tmp_path / "archive.bin"
    p.write_bytes(b"PK\x03\x04" + b"\x00" * 20)
    assert _detect_format(p) == "zip"


def test_detect_format_rar(tmp_path):
    p = tmp_path / "archive.bin"
    p.write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 20)
    assert _detect_format(p) == "rar"


def test_detect_format_7z(tmp_path):
    p = tmp_path / "archive.bin"
    p.write_bytes(b"7z\xbc\xaf'\x1c\x00\x04" + b"\x00" * 20)
    assert _detect_format(p) == "7z"


def test_detect_format_7z_inside_sfx(tmp_path):
    """A self-extracting exe wrapper: the 7z signature is buried, not at
    offset 0 — the sniffer must scan the first 512KB, not just the header."""
    p = tmp_path / "setup.exe"
    p.write_bytes(b"MZ" + b"\x00" * 1000 + b"7z\xbc\xaf'\x1c" + b"\x00" * 20)
    assert _detect_format(p) == "7z"


def test_detect_format_unknown(tmp_path):
    p = tmp_path / "archive.bin"
    p.write_bytes(b"not an archive at all")
    assert _detect_format(p) is None


def test_detect_format_missing_file_returns_none(tmp_path):
    assert _detect_format(tmp_path / "does-not-exist.bin") is None


# ── _find_executable ─────────────────────────────────────────────────
def test_find_executable_picks_the_only_exe(tmp_path):
    (tmp_path / "Game.exe").write_bytes(b"x" * 1000)
    assert _find_executable(str(tmp_path)) == str(tmp_path / "Game.exe")


def test_find_executable_filters_utility_keywords(tmp_path):
    (tmp_path / "Game.exe").write_bytes(b"x" * 1000)
    (tmp_path / "unins000.exe").write_bytes(b"x" * 1000)
    (tmp_path / "vcredist_x64.exe").write_bytes(b"x" * 1000)
    (tmp_path / "UE4PrereqSetup_x64.exe").write_bytes(b"x" * 1000)
    result = _find_executable(str(tmp_path))
    assert result == str(tmp_path / "Game.exe")


def test_find_executable_no_candidates_returns_none(tmp_path):
    (tmp_path / "readme.txt").write_text("hi")
    assert _find_executable(str(tmp_path)) is None


def test_find_executable_prefers_shallower_path(tmp_path):
    """Two otherwise-equal exes: the shallower one should win the
    depth-based score component."""
    (tmp_path / "Game.exe").write_bytes(b"x" * 1000)
    nested = tmp_path / "bin" / "redist" / "tools"
    nested.mkdir(parents=True)
    (nested / "Other.exe").write_bytes(b"x" * 1000)
    assert _find_executable(str(tmp_path)) == str(tmp_path / "Game.exe")


def test_find_executable_prefers_larger_file_at_equal_depth(tmp_path):
    (tmp_path / "Small.exe").write_bytes(b"x" * 1000)
    (tmp_path / "Big.exe").write_bytes(b"x" * (10 * 1024 * 1024))
    assert _find_executable(str(tmp_path)) == str(tmp_path / "Big.exe")


def test_find_executable_does_not_filter_trainer_or_cheat_by_name(tmp_path):
    """Regression documentation, not a desired behaviour: KNOWN GAP.

    ``_UTIL_KEYWORDS`` has no "trainer"/"cheat" entry, so a large trainer
    exe bundled inside a game's install dir can outscore (and thus
    replace) the real game exe purely on file size. This test pins the
    CURRENT behaviour so a future fix (adding those keywords, or scoping
    detection to the top-level dir only) has to consciously change this
    test rather than silently regress it back.
    """
    (tmp_path / "Game.exe").write_bytes(b"x" * (1 * 1024 * 1024))
    (tmp_path / "trainer.exe").write_bytes(b"x" * (20 * 1024 * 1024))
    # Today: the larger trainer.exe wins. This is the exact bug reproduced
    # live with Tempest Rising.
    assert _find_executable(str(tmp_path)) == str(tmp_path / "trainer.exe")


# ── install marker persistence ───────────────────────────────────────
def test_save_and_load_install_info_roundtrip(tmp_path, monkeypatch):
    import unifideck.stores.gamevault.install as install_mod
    monkeypatch.setattr(install_mod, "_MARKER_DIR", tmp_path)

    _save_install_info(
        "123", title="My Game", install_path="/games/mygame", exe_path="/games/mygame/Game.exe",
    )
    info = _load_install_info("123")
    assert info == {
        "game_id": "123",
        "title": "My Game",
        "install_path": "/games/mygame",
        "exe_path": "/games/mygame/Game.exe",
    }


def test_load_install_info_missing_returns_none(tmp_path, monkeypatch):
    import unifideck.stores.gamevault.install as install_mod
    monkeypatch.setattr(install_mod, "_MARKER_DIR", tmp_path)
    assert _load_install_info("does-not-exist") is None


def test_load_install_info_corrupt_json_returns_none(tmp_path, monkeypatch):
    import unifideck.stores.gamevault.install as install_mod
    monkeypatch.setattr(install_mod, "_MARKER_DIR", tmp_path)
    marker = tmp_path / "999.json"
    marker.write_text("{not valid json")
    assert _load_install_info("999") is None


def test_remove_install_info_deletes_marker(tmp_path, monkeypatch):
    import unifideck.stores.gamevault.install as install_mod
    monkeypatch.setattr(install_mod, "_MARKER_DIR", tmp_path)
    _save_install_info("42", title="T", install_path="/p", exe_path="/p/e.exe")
    assert (tmp_path / "42.json").exists()
    _remove_install_info("42")
    assert not (tmp_path / "42.json").exists()


def test_remove_install_info_missing_is_a_noop(tmp_path, monkeypatch):
    import unifideck.stores.gamevault.install as install_mod
    monkeypatch.setattr(install_mod, "_MARKER_DIR", tmp_path)
    _remove_install_info("never-existed")  # must not raise


def test_get_installed_reads_all_markers(tmp_path, monkeypatch):
    import unifideck.stores.gamevault.install as install_mod
    monkeypatch.setattr(install_mod, "_MARKER_DIR", tmp_path)
    _save_install_info("1", title="A", install_path="/a", exe_path="/a/a.exe")
    _save_install_info("2", title="B", install_path="/b", exe_path="/b/b.exe")

    installer = GameVaultInstaller(default_install_root="/root", download_dir="/dl")
    installed = installer.get_installed()

    assert set(installed.keys()) == {"1", "2"}
    assert installed["1"]["title"] == "A"


def test_get_installed_empty_dir_returns_empty(tmp_path, monkeypatch):
    import unifideck.stores.gamevault.install as install_mod
    monkeypatch.setattr(install_mod, "_MARKER_DIR", tmp_path)
    installer = GameVaultInstaller(default_install_root="/root", download_dir="/dl")
    assert installer.get_installed() == {}


def test_get_installed_missing_dir_returns_empty(tmp_path, monkeypatch):
    import unifideck.stores.gamevault.install as install_mod
    monkeypatch.setattr(install_mod, "_MARKER_DIR", tmp_path / "does-not-exist")
    installer = GameVaultInstaller(default_install_root="/root", download_dir="/dl")
    assert installer.get_installed() == {}


def test_get_install_info_delegates_to_module_function(tmp_path, monkeypatch):
    import unifideck.stores.gamevault.install as install_mod
    monkeypatch.setattr(install_mod, "_MARKER_DIR", tmp_path)
    _save_install_info("7", title="T", install_path="/p", exe_path="/p/e.exe")

    installer = GameVaultInstaller(default_install_root="/root", download_dir="/dl")
    assert installer.get_install_info("7")["title"] == "T"


# ── Content-Disposition filename parsing ─────────────────────────────
def test_parse_filename_from_cd_simple():
    assert _parse_filename_from_cd('attachment; filename="Game.zip"') == "Game.zip"


def test_parse_filename_from_cd_no_quotes():
    assert _parse_filename_from_cd("attachment; filename=Game.zip") == "Game.zip"


def test_parse_filename_from_cd_rfc5987_charset_prefix():
    """KNOWN GAP: the capture regex ``[^"\\';\\r\\n]+`` stops at the FIRST
    apostrophe, so it never actually reaches the ``''`` split branch for a
    real RFC 5987 ``filename*=UTF-8''...`` value — it captures only the
    charset token. Pinned as documentation of current behaviour, not the
    intended one.
    """
    header = "attachment; filename*=UTF-8''My%20Game.zip"
    assert _parse_filename_from_cd(header) == "UTF-8"


def test_parse_filename_from_cd_missing_returns_none():
    assert _parse_filename_from_cd("attachment") is None


def test_parse_filename_from_cd_empty_string_returns_none():
    assert _parse_filename_from_cd("") is None


# ── GameVaultInstaller construction ───────────────────────────────────
def test_installer_expands_user_paths():
    installer = GameVaultInstaller(
        default_install_root="~/Games/GameVault", download_dir="~/dl",
    )
    assert "~" not in str(installer._default_install_root)
    assert "~" not in str(installer._download_dir)


async def test_uninstall_game_missing_install_returns_failure(tmp_path, monkeypatch):
    import unifideck.stores.gamevault.install as install_mod
    monkeypatch.setattr(install_mod, "_MARKER_DIR", tmp_path)
    installer = GameVaultInstaller(default_install_root="/root", download_dir="/dl")

    result = await installer.uninstall_game("never-installed")

    assert result.success is False
    assert result.error == "Game not installed"


async def test_uninstall_game_removes_dir_and_marker(tmp_path, monkeypatch):
    import unifideck.stores.gamevault.install as install_mod
    monkeypatch.setattr(install_mod, "_MARKER_DIR", tmp_path)
    game_dir = tmp_path / "installed_game"
    game_dir.mkdir()
    (game_dir / "Game.exe").write_bytes(b"x")
    _save_install_info(
        "55", title="T", install_path=str(game_dir), exe_path=str(game_dir / "Game.exe"),
    )
    installer = GameVaultInstaller(default_install_root="/root", download_dir="/dl")

    result = await installer.uninstall_game("55")

    assert result.success is True
    assert not game_dir.exists()
    assert _load_install_info("55") is None
