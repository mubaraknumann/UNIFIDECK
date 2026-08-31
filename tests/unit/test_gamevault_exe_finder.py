"""Tests for ``stores.gamevault.exe_finder`` — picking the launch target.

Moved out of ``test_gamevault_install.py`` with the code, and extended for
native Linux builds, which local mode makes common: a vault folder on a Steam
Deck is routinely half native, and an ``.exe``-only scorer answered those with
``None`` — an install that reports success and can never launch.
"""
from __future__ import annotations

import os
import stat

from unifideck.stores.gamevault.exe_finder import find_executable


def _elf(path, size: int = 4096) -> None:
    """Write a file that looks and behaves like a native Linux binary."""
    path.write_bytes(b"\x7fELF" + b"\x00" * size)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


# ── Windows executables ──────────────────────────────────────────────
def test_find_executable_picks_the_only_exe(tmp_path):
    (tmp_path / "Game.exe").write_bytes(b"x" * 1000)
    assert find_executable(str(tmp_path)) == str(tmp_path / "Game.exe")


def test_find_executable_filters_utility_keywords(tmp_path):
    (tmp_path / "Game.exe").write_bytes(b"x" * 1000)
    (tmp_path / "unins000.exe").write_bytes(b"x" * 1000)
    (tmp_path / "vcredist_x64.exe").write_bytes(b"x" * 1000)
    (tmp_path / "UE4PrereqSetup_x64.exe").write_bytes(b"x" * 1000)
    assert find_executable(str(tmp_path)) == str(tmp_path / "Game.exe")


def test_find_executable_no_candidates_returns_none(tmp_path):
    (tmp_path / "readme.txt").write_text("hi")
    assert find_executable(str(tmp_path)) is None


def test_find_executable_prefers_shallower_path(tmp_path):
    """Two otherwise-equal exes: the shallower one wins on depth score.

    The nested path deliberately avoids ``_PRUNE_DIRS`` names, or the walk
    would skip it and the test would pass without exercising the scoring it
    claims to.
    """
    (tmp_path / "Game.exe").write_bytes(b"x" * 1000)
    nested = tmp_path / "engine" / "bin" / "tools"
    nested.mkdir(parents=True)
    (nested / "Other.exe").write_bytes(b"x" * 1000)
    assert find_executable(str(tmp_path)) == str(tmp_path / "Game.exe")


def test_find_executable_prefers_larger_file_at_equal_depth(tmp_path):
    (tmp_path / "Small.exe").write_bytes(b"x" * 1000)
    (tmp_path / "Big.exe").write_bytes(b"x" * (10 * 1024 * 1024))
    assert find_executable(str(tmp_path)) == str(tmp_path / "Big.exe")


def test_find_executable_does_not_filter_trainer_or_cheat_by_name(tmp_path):
    """Regression documentation, not a desired behaviour: KNOWN GAP.

    ``_UTIL_KEYWORDS`` has no "trainer"/"cheat" entry, so a large trainer
    exe bundled inside a game's install dir can outscore (and thus replace)
    the real game exe purely on file size. This test pins the CURRENT
    behaviour so a future fix has to consciously change it rather than
    silently regress.
    """
    (tmp_path / "Game.exe").write_bytes(b"x" * (1 * 1024 * 1024))
    (tmp_path / "trainer.exe").write_bytes(b"x" * (20 * 1024 * 1024))
    assert find_executable(str(tmp_path)) == str(tmp_path / "trainer.exe")


def test_prune_dirs_keeps_redist_binaries_out_of_the_running(tmp_path):
    """A redist folder is skipped wholesale, not merely demoted."""
    redist = tmp_path / "_CommonRedist" / "vcredist"
    redist.mkdir(parents=True)
    (redist / "Huge.exe").write_bytes(b"x" * (50 * 1024 * 1024))
    (tmp_path / "Game.exe").write_bytes(b"x" * 1000)
    assert find_executable(str(tmp_path)) == str(tmp_path / "Game.exe")


# ── Degrade, don't eliminate ─────────────────────────────────────────
#
# The keyword filter expresses a preference. When it rejects every
# candidate, returning None produced an install that reported success and
# could never launch: the first real GameVault install extracted a repack
# whose only executable was ``Setup.exe``, the filter rejected it on
# "setup", the marker got ``exe_path: ""`` and reconcile logged
#
#   mark_installed gamevault:1 — empty exe_path; launcher will not be able
#   to resolve a target
def test_find_executable_falls_back_when_everything_is_filtered(tmp_path):
    """A repack whose only executable is its installer."""
    repack = tmp_path / "Ghost of Tsushima [DODI Repack]"
    repack.mkdir()
    (repack / "Setup.exe").write_bytes(b"x" * 8_000_000)

    assert find_executable(str(tmp_path)) == str(repack / "Setup.exe")


def test_find_executable_still_prefers_a_real_game_exe(tmp_path):
    """The fallback must not weaken the preference when both exist."""
    (tmp_path / "Setup.exe").write_bytes(b"x" * 9_000_000)
    (tmp_path / "GhostOfTsushima.exe").write_bytes(b"x" * 1000)

    assert find_executable(str(tmp_path)) == str(tmp_path / "GhostOfTsushima.exe")


def test_find_executable_returns_none_when_there_is_no_exe(tmp_path):
    (tmp_path / "readme.txt").write_text("no executables here")

    assert find_executable(str(tmp_path)) is None


# ── Native Linux builds ──────────────────────────────────────────────
def test_finds_a_shell_launcher(tmp_path):
    (tmp_path / "start.sh").write_text("#!/bin/sh\nexec ./game\n")
    assert find_executable(str(tmp_path)) == str(tmp_path / "start.sh")


def test_finds_an_appimage(tmp_path):
    (tmp_path / "Celeste.AppImage").write_bytes(b"x" * 5000)
    assert find_executable(str(tmp_path)) == str(tmp_path / "Celeste.AppImage")


def test_finds_an_executable_elf_with_no_extension(tmp_path):
    _elf(tmp_path / "BabaIsYou")
    assert find_executable(str(tmp_path)) == str(tmp_path / "BabaIsYou")


def test_ignores_an_elf_that_is_not_executable(tmp_path):
    """A shipped ``.so``-alike with no ``+x`` bit is not a launch target."""
    (tmp_path / "libdata").write_bytes(b"\x7fELF" + b"\x00" * 4096)
    os.chmod(tmp_path / "libdata", 0o644)
    assert find_executable(str(tmp_path)) is None


def test_ignores_a_non_elf_extensionless_file(tmp_path):
    blob = tmp_path / "gamedata"
    blob.write_bytes(b"NOTELF" + b"\x00" * 100)
    blob.chmod(0o755)
    assert find_executable(str(tmp_path)) is None


def test_conventional_launcher_beats_a_bare_elf(tmp_path):
    _elf(tmp_path / "game_bin", size=20_000_000)
    (tmp_path / "start.sh").write_text("#!/bin/sh\n")
    assert find_executable(str(tmp_path)) == str(tmp_path / "start.sh")


def test_prefer_native_reorders_a_mixed_archive(tmp_path):
    """An archive labelled ``(L_P)`` that also ships a Windows build."""
    (tmp_path / "Game.exe").write_bytes(b"x" * (50 * 1024 * 1024))
    _elf(tmp_path / "Game")

    assert find_executable(str(tmp_path), prefer_native=True) == str(
        tmp_path / "Game",
    )
    assert find_executable(str(tmp_path), prefer_native=False) == str(
        tmp_path / "Game.exe",
    )


def test_prefer_native_still_falls_back_to_windows_when_mislabelled(tmp_path):
    """A wrong type token must not make the game unlaunchable."""
    (tmp_path / "Game.exe").write_bytes(b"x" * 1000)
    assert find_executable(str(tmp_path), prefer_native=True) == str(
        tmp_path / "Game.exe",
    )
