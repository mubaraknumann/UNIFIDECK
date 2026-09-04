"""Unit tests for latest-GE-Proton install + selector default tier.

Covers the three pieces added for "always run the latest GE-Proton,
fall back to Proton Experimental":

* ``ge_installer`` — latest-tag lookup, broken-extract detection,
  marker cache, and the "already installed → no download" short-circuit.
* ``selector`` — official-tool dir aliasing (so ``proton_experimental``
  resolves to ``Proton - Experimental``) and the new default tier
  (cached latest GE → on-demand download → Experimental → raise).
* ``ProtonService`` — no longer forces a per-store compat tool by
  default, but still honours an explicit ctor override.
"""
from __future__ import annotations

import json
import stat
import urllib.error
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from unifideck.launcher.proton.infrastructure import ge_installer, selector
from unifideck.launcher.types.errors import ProtonUnavailableError


def _make_proton(dir_path: Path, *, executable: bool) -> Path:
    """Create ``<dir_path>/proton`` with/without the +x bit."""
    dir_path.mkdir(parents=True, exist_ok=True)
    proton = dir_path / "proton"
    proton.write_text("#!/bin/sh\n")
    mode = proton.stat().st_mode
    exec_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    proton.chmod(mode | exec_bits if executable else mode & ~exec_bits)
    return proton


# ── ge_installer.is_valid_ge_install ──────────────────────────────

def test_is_valid_ge_install_true_when_executable(tmp_path, monkeypatch):
    root = tmp_path / "compatibilitytools.d"
    proton = _make_proton(root / "GE-Proton10-34", executable=True)
    monkeypatch.setattr(ge_installer, "_SCAN_ROOTS", (str(root),))

    assert ge_installer.is_valid_ge_install("GE-Proton10-34") is True
    assert ge_installer.installed_ge_proton_path("GE-Proton10-34") == proton


def test_is_valid_ge_install_false_when_not_executable(tmp_path, monkeypatch):
    # Mirrors the real broken GE-Proton10-34 on disk (proton is 0644):
    # present but non-executable → must be treated as NOT installed.
    root = tmp_path / "compatibilitytools.d"
    _make_proton(root / "GE-Proton10-34", executable=False)
    monkeypatch.setattr(ge_installer, "_SCAN_ROOTS", (str(root),))

    assert ge_installer.is_valid_ge_install("GE-Proton10-34") is False
    assert ge_installer.installed_ge_proton_path("GE-Proton10-34") is None


def test_is_valid_ge_install_false_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(ge_installer, "_SCAN_ROOTS", (str(tmp_path),))
    assert ge_installer.is_valid_ge_install("GE-Proton99-99") is False


# ── ge_installer.get_latest_ge_tag ────────────────────────────────

def test_get_latest_ge_tag_success():
    payload = json.dumps({"tag_name": "GE-Proton10-34"}).encode()
    with patch("urllib.request.urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value.read.return_value = payload
        assert ge_installer.get_latest_ge_tag() == "GE-Proton10-34"


def test_get_latest_ge_tag_network_failure_returns_none():
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("offline"),
    ):
        assert ge_installer.get_latest_ge_tag() is None


# ── ge_installer._select_tarball ───────────────────────────────────

def test_select_tarball_ignores_aarch64_and_prefers_exact_tag():
    assets = [
        {"name": "GE-Proton11-1-aarch64.sha512sum", "browser_download_url": "http://example.com/arm.sha"},
        {"name": "GE-Proton11-1-aarch64.tar.gz", "browser_download_url": "http://example.com/arm.tar.gz"},
        {"name": "GE-Proton11-1.sha512sum", "browser_download_url": "http://example.com/x86.sha"},
        {"name": "GE-Proton11-1.tar.gz", "browser_download_url": "http://example.com/x86.tar.gz"},
    ]
    url = ge_installer._select_tarball(assets, "GE-Proton11-1")
    assert url == "http://example.com/x86.tar.gz"


def test_select_tarball_fallback_without_tag():
    assets = [
        {"name": "GE-Proton11-1-aarch64.tar.gz", "browser_download_url": "http://example.com/arm.tar.gz"},
        {"name": "GE-Proton11-1.tar.gz", "browser_download_url": "http://example.com/x86.tar.gz"},
    ]
    url = ge_installer._select_tarball(assets)
    assert url == "http://example.com/x86.tar.gz"


# ── ge_installer marker + ensure_latest_ge ────────────────────────


def test_marker_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(ge_installer, "_MARKER", tmp_path / "latest.json")
    assert ge_installer.read_cached_latest_tag() is None
    ge_installer._write_marker("GE-Proton10-34")
    assert ge_installer.read_cached_latest_tag() == "GE-Proton10-34"


def test_ensure_latest_ge_uses_existing_without_download(tmp_path, monkeypatch):
    monkeypatch.setattr(ge_installer, "_MARKER", tmp_path / "latest.json")
    existing = tmp_path / "GE-Proton10-34" / "proton"
    monkeypatch.setattr(
        ge_installer, "_fetch_latest_release",
        lambda timeout: {"tag_name": "GE-Proton10-34"},
    )
    monkeypatch.setattr(
        ge_installer, "installed_ge_proton_path", lambda tag: existing,
    )
    download = MagicMock()
    monkeypatch.setattr(ge_installer, "_download_and_install", download)

    assert ge_installer.ensure_latest_ge() == (existing, "GE-Proton10-34")
    download.assert_not_called()
    # Marker refreshed so the launcher can skip the network next time.
    assert ge_installer.read_cached_latest_tag() == "GE-Proton10-34"


def test_ensure_latest_ge_offline_returns_none(monkeypatch):
    monkeypatch.setattr(ge_installer, "_fetch_latest_release", lambda timeout: None)
    assert ge_installer.ensure_latest_ge() is None


# ── selector.resolve_proton_path (official-tool aliasing) ─────────

def _point_selector_roots(tmp_path, monkeypatch):
    compat = tmp_path / "compat"
    lib = tmp_path / "common"
    empty = tmp_path / "empty"
    for d in (compat, lib, empty):
        d.mkdir()
    monkeypatch.setattr(selector, "UNIFIDECK_COMPAT_DIR", str(empty))
    monkeypatch.setattr(selector, "STEAM_COMPAT_ROOTS", [str(compat)])
    monkeypatch.setattr(selector, "STEAM_LIBRARY_ROOTS", [str(lib)])
    # Proton is also searched across every library in libraryfolders.vdf;
    # stub that out so the host's real Steam install can't satisfy a lookup
    # these tests expect to resolve inside tmp_path (or not at all).
    monkeypatch.setattr(selector, "_discovered_library_commons", lambda: [])
    return compat, lib


def test_resolve_proton_path_aliases_experimental(tmp_path, monkeypatch):
    _compat, lib = _point_selector_roots(tmp_path, monkeypatch)
    proton = _make_proton(lib / "Proton - Experimental", executable=True)
    # The tool id is ``proton_experimental`` but the dir is the display
    # name — without the alias map this returned None (the original bug).
    assert selector.resolve_proton_path("proton_experimental") == proton


def test_resolve_proton_path_ge_in_compat_root(tmp_path, monkeypatch):
    compat, _lib = _point_selector_roots(tmp_path, monkeypatch)
    proton = _make_proton(compat / "GE-Proton10-34", executable=True)
    assert selector.resolve_proton_path("GE-Proton10-34") == proton


def test_resolve_proton_path_unknown_returns_none(tmp_path, monkeypatch):
    _point_selector_roots(tmp_path, monkeypatch)
    assert selector.resolve_proton_path("does-not-exist") is None


# ── selector.get_saved_proton_tool (pre-0.7.0 schema compat) ──────

def _write_proton_settings(tmp_path, monkeypatch, games: dict) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".local" / "share" / "unifideck" / "proton_settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"games": games}))


def test_get_saved_proton_tool_reads_current_string_schema(tmp_path, monkeypatch):
    _write_proton_settings(tmp_path, monkeypatch, {"gog:1": "GE-Proton10-34"})
    assert selector.get_saved_proton_tool("gog:1") == "GE-Proton10-34"


def test_get_saved_proton_tool_ignores_legacy_dict_schema(tmp_path, monkeypatch):
    """Regression: a pre-0.7.0 dict-shaped entry ({"proton_tool": "<id>"},
    see the retired bash launcher's get_unifideck_proton_tool) must not be
    extracted and honored as the current choice. useLaunchPrep only
    refreshes this file when the game-details page is opened, not on
    every launch — so a legacy entry is stale data from before the
    rewrite, possibly a long-forgotten pin the user can't even see is in
    effect anymore (Steam's own Force-Compat UI reflects whatever was
    last restored there, independent of this file). Real field case: a
    game silently kept launching on a much older GE-Proton than the
    reporter's actual current default, with no visible error. Treat any
    dict shape as "no saved override" so the normal priority chain
    (Steam override -> Unifideck default -> latest GE-Proton) applies.
    """
    _write_proton_settings(
        tmp_path, monkeypatch, {"gog:1": {"proton_tool": "GE-Proton10-34"}},
    )
    assert selector.get_saved_proton_tool("gog:1") is None


def test_get_saved_proton_tool_legacy_dict_missing_key_is_none(tmp_path, monkeypatch):
    _write_proton_settings(tmp_path, monkeypatch, {"gog:1": {}})
    assert selector.get_saved_proton_tool("gog:1") is None


def test_select_proton_version_falls_through_legacy_dict_entry(
    tmp_path, monkeypatch,
):
    """Regression: a pre-0.7.0 dict-shaped entry must not raise TypeError
    (the original field crash: ``resolve_proton_path`` received a dict
    instead of a str tool id and blew up on ``Path(...) / tool_id``) AND
    must not resurrect the stale pinned version — it should fall through
    to the next tier exactly as if no saved override existed.
    """
    _write_proton_settings(
        tmp_path, monkeypatch, {"gog:1": {"proton_tool": "GE-Proton10-34"}},
    )
    monkeypatch.setattr(selector, "get_steam_compat_tool_override", lambda aid: None)
    monkeypatch.setattr(selector, "get_unifideck_proton_tool", lambda: None)
    proton = tmp_path / "GE-Proton11-1" / "proton"
    monkeypatch.setattr(
        selector.ge_installer, "read_cached_latest_tag", lambda: "GE-Proton11-1",
    )
    monkeypatch.setattr(
        selector.ge_installer, "installed_ge_proton_path",
        lambda tag: proton if tag == "GE-Proton11-1" else None,
    )

    assert selector.select_proton_version(store_game_id="gog:1") == (
        proton, "GE-Proton11-1",
    )


# ── selector.select_proton_version (default tier) ─────────────────

def _silence_higher_tiers(monkeypatch):
    monkeypatch.setattr(selector, "get_saved_proton_tool", lambda gid: None)
    monkeypatch.setattr(selector, "get_steam_compat_tool_override", lambda aid: None)
    monkeypatch.setattr(selector, "get_unifideck_proton_tool", lambda: None)
    monkeypatch.setattr(selector, "get_global_default_tool", lambda: None)


def test_select_prefers_cached_latest_ge(tmp_path, monkeypatch):
    _silence_higher_tiers(monkeypatch)
    proton = tmp_path / "GE-Proton10-34" / "proton"
    monkeypatch.setattr(
        selector.ge_installer, "read_cached_latest_tag", lambda: "GE-Proton10-34",
    )
    monkeypatch.setattr(
        selector.ge_installer, "installed_ge_proton_path",
        lambda tag: proton if tag == "GE-Proton10-34" else None,
    )
    ensure = MagicMock()
    monkeypatch.setattr(selector.ge_installer, "ensure_latest_ge", ensure)

    assert selector.select_proton_version() == (proton, "GE-Proton10-34")
    ensure.assert_not_called()  # cached → no network/download


def test_select_downloads_latest_when_not_cached(tmp_path, monkeypatch):
    _silence_higher_tiers(monkeypatch)
    proton = tmp_path / "GE-Proton10-34" / "proton"
    monkeypatch.setattr(selector.ge_installer, "read_cached_latest_tag", lambda: None)
    monkeypatch.setattr(
        selector.ge_installer, "ensure_latest_ge",
        lambda progress_cb=None: (proton, "GE-Proton10-34"),
    )
    assert selector.select_proton_version() == (proton, "GE-Proton10-34")


def test_select_falls_back_to_experimental_when_offline(tmp_path, monkeypatch):
    _silence_higher_tiers(monkeypatch)
    _compat, lib = _point_selector_roots(tmp_path, monkeypatch)
    exp = _make_proton(lib / "Proton - Experimental", executable=True)
    monkeypatch.setattr(selector.ge_installer, "read_cached_latest_tag", lambda: None)
    monkeypatch.setattr(
        selector.ge_installer, "ensure_latest_ge", lambda progress_cb=None: None,
    )

    assert selector.select_proton_version() == (exp, "proton_experimental")


def test_select_raises_when_nothing_available(tmp_path, monkeypatch):
    _silence_higher_tiers(monkeypatch)
    _point_selector_roots(tmp_path, monkeypatch)  # no Experimental on disk
    monkeypatch.setattr(selector.ge_installer, "read_cached_latest_tag", lambda: None)
    monkeypatch.setattr(
        selector.ge_installer, "ensure_latest_ge", lambda progress_cb=None: None,
    )

    with pytest.raises(ProtonUnavailableError):
        selector.select_proton_version()


# ── launch-time GE-download toasts ────────────────────────────────

def test_download_announcer_toasts_once(monkeypatch):
    """The progress callback toasts on the first chunk, then stays quiet."""
    import unifideck.launcher.frontend_bridge as fb
    spy = MagicMock()
    monkeypatch.setattr(fb, "launcher_toast", spy)

    cb = selector._GeDownloadAnnouncer()
    cb(1024, 9999)
    cb(2048, 9999)
    cb(4096, 9999)

    assert cb.fired is True
    spy.assert_called_once()
    assert spy.call_args.args[0] == "toasts.launcher.downloadingProton"


def test_select_toasts_when_download_happens(tmp_path, monkeypatch):
    """A real launch-time download fires download + ready toasts."""
    _silence_higher_tiers(monkeypatch)
    import unifideck.launcher.frontend_bridge as fb
    spy = MagicMock()
    monkeypatch.setattr(fb, "launcher_toast", spy)
    proton = tmp_path / "GE-Proton10-34" / "proton"
    monkeypatch.setattr(selector.ge_installer, "read_cached_latest_tag", lambda: None)

    def _ensure(progress_cb=None):
        # Simulate streaming bytes so the announcer fires.
        if progress_cb:
            progress_cb(1024, 2048)
        return proton, "GE-Proton10-34"

    monkeypatch.setattr(selector.ge_installer, "ensure_latest_ge", _ensure)

    assert selector.select_proton_version() == (proton, "GE-Proton10-34")
    keys = [c.args[0] for c in spy.call_args_list]
    assert "toasts.launcher.downloadingProton" in keys
    assert "toasts.launcher.protonReadyBody" in keys


def test_select_silent_when_no_download(tmp_path, monkeypatch):
    """No download (cb never fires) → no GE toasts."""
    _silence_higher_tiers(monkeypatch)
    import unifideck.launcher.frontend_bridge as fb
    spy = MagicMock()
    monkeypatch.setattr(fb, "launcher_toast", spy)
    proton = tmp_path / "GE-Proton10-34" / "proton"
    monkeypatch.setattr(selector.ge_installer, "read_cached_latest_tag", lambda: None)
    # Already-installed path: ensure_latest_ge returns without streaming.
    monkeypatch.setattr(
        selector.ge_installer, "ensure_latest_ge",
        lambda progress_cb=None: (proton, "GE-Proton10-34"),
    )

    selector.select_proton_version()
    keys = [c.args[0] for c in spy.call_args_list]
    assert "toasts.launcher.downloadingProton" not in keys
    assert "toasts.launcher.protonReadyBody" not in keys


# ── umu runtime first-setup toast ─────────────────────────────────

def test_umu_runtime_toasts_when_steamrt3_missing(tmp_path, monkeypatch):
    from unifideck.launcher.proton.infrastructure import umu_runtime
    spy = MagicMock()
    monkeypatch.setattr(umu_runtime, "launcher_toast", spy)
    monkeypatch.setattr(umu_runtime, "UMU_CACHE_DIR", tmp_path / "umu")
    monkeypatch.setenv("HOME", str(tmp_path))  # contain ~/.config/umu

    umu_runtime.ensure_umu_runtime_ready()

    spy.assert_called_once()
    assert spy.call_args.args[0] == "toasts.launcher.downloadingRuntime"


def test_umu_runtime_silent_when_steamrt3_present(tmp_path, monkeypatch):
    from unifideck.launcher.proton.infrastructure import umu_runtime
    spy = MagicMock()
    monkeypatch.setattr(umu_runtime, "launcher_toast", spy)
    cache = tmp_path / "umu"
    (cache / "steamrt3").mkdir(parents=True)
    monkeypatch.setattr(umu_runtime, "UMU_CACHE_DIR", cache)
    monkeypatch.setenv("HOME", str(tmp_path))

    umu_runtime.ensure_umu_runtime_ready()

    spy.assert_not_called()


def test_umu_runtime_silent_when_steamrt4_present(tmp_path, monkeypatch):
    """Regression: a newer Proton build can resolve umu to steamrt4, not
    the default steamrt3 — the readiness check must recognize either."""
    from unifideck.launcher.proton.infrastructure import umu_runtime
    spy = MagicMock()
    monkeypatch.setattr(umu_runtime, "launcher_toast", spy)
    cache = tmp_path / "umu"
    (cache / "steamrt4").mkdir(parents=True)
    monkeypatch.setattr(umu_runtime, "UMU_CACHE_DIR", cache)
    monkeypatch.setenv("HOME", str(tmp_path))

    umu_runtime.ensure_umu_runtime_ready()

    spy.assert_not_called()


async def test_run_umu_with_retry_recovers_from_exit_127(tmp_path, monkeypatch):
    """Regression (refined by UD-022): rc=127 is still *recoverable* — the
    retry loop tries a second time rather than giving up on attempt 1 — but
    it must NOT wipe the shared steamrt runtime cache. 127 is exec/command-
    not-found, not a runtime-corruption signal; only 2/74 justify the
    multi-hundred-MB shared-cache nuke. See ``_RUNTIME_CORRUPTION_CODES``.
    """
    from unifideck.launcher.proton.infrastructure import umu_runtime

    counter = tmp_path / "attempts"
    script = tmp_path / "flaky.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'n=$(cat "{counter}" 2>/dev/null || echo 0)\n'
        f'echo $((n + 1)) > "{counter}"\n'
        'if [ "$n" -eq 0 ]; then exit 127; fi\n'
        "exit 0\n",
    )
    script.chmod(0o755)
    cleanup = MagicMock()
    monkeypatch.setattr(umu_runtime, "cleanup_umu_runtime_cache", cleanup)

    rc = await umu_runtime.run_umu_with_retry([str(script)], max_attempts=2)

    assert rc == 0
    assert counter.read_text().strip() == "2"  # retried once → recovered
    cleanup.assert_not_called()  # 127 must NOT nuke the shared runtime cache


def test_cleanup_umu_runtime_cache_wipes_steamrt4(tmp_path, monkeypatch):
    """Regression: cache cleanup used to only ever target "steamrt3",
    silently no-op'ing for anyone whose Proton build resolved umu to
    steamrt4 — the retry-and-wipe self-heal never actually fixed anything
    for them. Must wipe whichever runtime variant(s) are actually present.
    """
    from unifideck.launcher.proton.infrastructure import umu_runtime
    cache = tmp_path / "umu"
    (cache / "steamrt4" / "pressure-vessel").mkdir(parents=True)
    monkeypatch.setattr(umu_runtime, "UMU_CACHE_DIR", cache)

    umu_runtime.cleanup_umu_runtime_cache()

    assert not (cache / "steamrt4").exists()


# ── ProtonService writes no compat-tool entries ───────────────────

async def test_proton_service_holds_no_compat_tool_surface():
    """ProtonService only keeps GE-Proton installed — it writes no config.vdf.

    The old ``GAME_INSTALLED`` → ``set_compat_tool`` path was removed: the
    event had no live emitter, its payload key never matched what the handler
    read, the per-store tool table was empty for every store, and in the
    plugin it was pointed at ``localconfig.vdf`` while ``CompatToolMapping``
    lives in ``config/config.vdf``. ``ProtonToolsManager`` in
    ``compatibility/proton_helpers.py`` is the one live writer. Asserting the
    surface is gone keeps it from being reintroduced by copy-paste.
    """
    from unifideck.services.proton_service import ProtonService

    svc = ProtonService(MagicMock())

    for gone in (
        "set_compat_tool",
        "_inject_compat_tool",
        "_on_game_installed",
        "set_config_vdf_path",
    ):
        assert not hasattr(svc, gone), f"{gone} should have been removed"


# ── emit_stage payload forwarding ─────────────────────────────────

async def test_emit_stage_forwards_optional_fields():
    from unifideck.core.types import Events
    from unifideck.launcher.rpc import emit_stage

    bus = MagicMock()
    bus.emit = AsyncMock()
    await emit_stage(
        bus,
        i18n_key="toasts.launcher.protonSwitchedTo",
        i18n_title_key="toasts.launcher.protonUpgrade",
        game_title="epic:1",
        i18n_params={"version": "GE-Proton10-34"},
        severity="info",
        priority="normal",
    )
    bus.emit.assert_awaited_once()
    args, kwargs = bus.emit.call_args
    assert args[0] == Events.LAUNCHER_STAGE
    assert kwargs["i18n_title_key"] == "toasts.launcher.protonUpgrade"
    assert kwargs["i18n_key"] == "toasts.launcher.protonSwitchedTo"
    assert kwargs["i18n_params"] == {"version": "GE-Proton10-34"}
    assert kwargs["severity"] == "info"


async def test_emit_stage_omits_unset_optionals():
    from unifideck.launcher.rpc import emit_stage

    bus = MagicMock()
    bus.emit = AsyncMock()
    await emit_stage(bus, i18n_key="toasts.launcher.launchingGame", game_title="g")
    _args, kwargs = bus.emit.call_args
    assert "i18n_title_key" not in kwargs
    assert "i18n_params" not in kwargs
    assert "severity" not in kwargs


# ── Externally managed GE-Proton detection + notification ──────────

def test_parse_ge_version():
    from unifideck.launcher.proton.infrastructure import external_ge

    assert external_ge.parse_ge_version("GE-Proton11-5") == (11, 5)
    assert external_ge.parse_ge_version("GE-Proton10-34") == (10, 34)
    assert external_ge.parse_ge_version("GE-Proton8_25") == (8, 25)
    assert external_ge.parse_ge_version("Proton-10") is None
    assert external_ge.parse_ge_version("") is None


def test_is_ge_outdated():
    from unifideck.launcher.proton.infrastructure import external_ge

    assert external_ge.is_ge_outdated("GE-Proton10-34", "GE-Proton11-5") is True
    assert external_ge.is_ge_outdated("GE-Proton11-4", "GE-Proton11-5") is True
    assert external_ge.is_ge_outdated("GE-Proton11-5", "GE-Proton11-5") is False
    assert external_ge.is_ge_outdated("GE-Proton11-6", "GE-Proton11-5") is False
    assert external_ge.is_ge_outdated("unknown", "GE-Proton11-5") is False


def test_is_ge_sufficiently_fresh():
    from unifideck.launcher.proton.infrastructure import external_ge

    assert external_ge.is_ge_sufficiently_fresh("GE-Proton11-5", "GE-Proton11-5") is True
    assert external_ge.is_ge_sufficiently_fresh("GE-Proton11-1", "GE-Proton11-5") is True
    assert external_ge.is_ge_sufficiently_fresh("GE-Proton11-0", "GE-Proton11-5") is True
    assert external_ge.is_ge_sufficiently_fresh("GE-Proton11-1", "GE-Proton11-7") is False
    assert external_ge.is_ge_sufficiently_fresh("GE-Proton10-34", "GE-Proton11-1") is False
    assert external_ge.is_ge_sufficiently_fresh("GE-Proton11-6", "GE-Proton11-5") is True
    assert external_ge.is_ge_sufficiently_fresh("unknown", "GE-Proton11-5") is False
    assert external_ge.is_ge_sufficiently_fresh("GE-Proton11-5", "invalid") is False


def test_find_external_ge_proton_detects_via_manifest_display_name(tmp_path):
    """Manifest declares 'Proton-GE Latest' as display_name while dir is named differently."""
    from unifideck.launcher.proton.infrastructure import external_ge

    root = tmp_path / "compatibilitytools.d"
    tool_dir = root / "GE-Proton-custom"
    tool_dir.mkdir(parents=True)
    _make_proton(tool_dir, executable=True)
    (tool_dir / "files" / "bin").mkdir(parents=True)
    (tool_dir / "files" / "bin" / "wine").write_text("")
    (tool_dir / "version").write_text("1724000000 GE-Proton11-3\n")
    (tool_dir / "toolmanifest.vdf").write_text('"manifest" { "commandline" "/proton" }')
    (tool_dir / "compatibilitytool.vdf").write_text(
        '"compatibilitytools" {\n'
        '  "compat_tools" {\n'
        '    "custom_ge" {\n'
        '      "display_name" "Proton-GE Latest"\n'
        '      "install_path" "."\n'
        '    }\n'
        '  }\n'
        '}'
    )

    result = external_ge.find_external_ge_proton(roots=[root])
    assert result is not None
    proton_script, alias_name, real_ver = result
    assert proton_script == tool_dir / "proton"
    assert alias_name == "Proton-GE Latest"
    assert real_ver == "GE-Proton11-3"


def test_find_external_ge_proton_detects_alias_dir(tmp_path):
    """Directory itself is named 'Proton-GE Latest'."""
    from unifideck.launcher.proton.infrastructure import external_ge

    root = tmp_path / "compatibilitytools.d"
    alias_dir = root / "Proton-GE Latest"
    alias_dir.mkdir(parents=True)
    _make_proton(alias_dir, executable=True)
    (alias_dir / "files" / "bin").mkdir(parents=True)
    (alias_dir / "files" / "bin" / "wine").write_text("")
    (alias_dir / "version").write_text("GE-Proton11-4\n")
    (alias_dir / "toolmanifest.vdf").write_text('"manifest" { "commandline" "/proton" }')

    result = external_ge.find_external_ge_proton(roots=[root])
    assert result is not None
    proton_script, alias_name, real_ver = result
    assert proton_script == alias_dir / "proton"
    assert alias_name == "Proton-GE Latest"
    assert real_ver == "GE-Proton11-4"


def test_selector_prefers_external_ge_when_up_to_date(tmp_path, monkeypatch):
    from unifideck.launcher.proton.infrastructure import external_ge, selector

    fake_ext_proton = tmp_path / "Proton-GE Latest" / "proton"
    monkeypatch.setattr(
        external_ge, "find_external_ge_proton",
        lambda roots=None: (fake_ext_proton, "Proton-GE Latest", "GE-Proton11-5"),
    )
    monkeypatch.setattr(
        ge_installer, "read_cached_latest_tag",
        lambda: "GE-Proton11-3",
    )
    monkeypatch.setattr(
        ge_installer, "installed_ge_proton_path",
        lambda tag: tmp_path / "unifideck-ge" / "proton",
    )

    tried = []
    path, tool = selector._default_latest_ge(tried)
    assert path == fake_ext_proton
    assert tool == "Proton-GE Latest"
    assert "external-ge:Proton-GE Latest" in tried


def test_selector_prefers_cached_unifideck_ge_when_external_is_older(tmp_path, monkeypatch):
    from unifideck.launcher.proton.infrastructure import external_ge, selector

    fake_ext_proton = tmp_path / "Proton-GE Latest" / "proton"
    fake_cached_proton = tmp_path / "unifideck-ge" / "proton"
    monkeypatch.setattr(
        external_ge, "find_external_ge_proton",
        lambda roots=None: (fake_ext_proton, "Proton-GE Latest", "GE-Proton10-34"),
    )
    monkeypatch.setattr(
        ge_installer, "read_cached_latest_tag",
        lambda: "GE-Proton11-5",
    )
    monkeypatch.setattr(
        ge_installer, "installed_ge_proton_path",
        lambda tag: fake_cached_proton,
    )

    tried = []
    path, tool = selector._default_latest_ge(tried)
    assert path == fake_cached_proton
    assert tool == "GE-Proton11-5"
    assert "latest-ge-cached:GE-Proton11-5" in tried


async def test_ge_fallback_recovers_from_external_ge_failure(tmp_path, monkeypatch):
    """External GE failure must still fall back to bundled GE-Proton (identity check)."""
    import sys
    sys.modules.setdefault("aiohttp", MagicMock())

    from unifideck.launcher.proton.compat import ge_fallback

    bundled_proton = tmp_path / "bundled-ge" / "proton"
    monkeypatch.setattr(
        ge_fallback, "_resolve_ge_proton",
        lambda: (bundled_proton, "GE-Proton11-5"),
    )
    run_createprefix = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "unifideck.launcher.proton.compat.prefix_init._run_createprefix_with_retry",
        run_createprefix,
    )
    monkeypatch.setattr(
        "unifideck.launcher.proton.compat.save_migration.restore_or_migrate_saves",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "unifideck.compatibility.proton_helpers.save_proton_setting",
        MagicMock(),
    )
    monkeypatch.setattr(
        "unifideck.launcher.frontend_bridge.launcher_toast",
        MagicMock(),
    )

    plan = MagicMock()
    plan.state.proton_tool_id = "Proton-GE Latest"
    plan.state.proton_path = tmp_path / "external-ge" / "proton"
    plan.context.store = "epic"
    plan.context.game_id = "1"
    plan.context.game_key = "epic:1"
    plan.python_bin = Path("/usr/bin/python3")
    plan.on_process_start = None

    prefix_root = tmp_path / "prefix"
    prefix_root.mkdir(parents=True)

    await ge_fallback.fallback_to_ge_proton(plan, prefix_root)
    run_createprefix.assert_awaited_once()


async def test_proton_service_external_ge_outdated_emits_toast(monkeypatch):
    from unifideck.launcher.proton.infrastructure import external_ge
    from unifideck.services.proton_service import ProtonService

    fake_proton = Path("/fake/Proton-GE Latest/proton")
    monkeypatch.setattr(
        external_ge, "find_external_ge_proton",
        lambda roots=None: (fake_proton, "Proton-GE Latest", "GE-Proton10-34"),
    )
    monkeypatch.setattr(
        ge_installer, "get_latest_ge_tag",
        lambda timeout=8.0: "GE-Proton11-5",
    )
    monkeypatch.setattr(
        ge_installer, "is_valid_ge_install",
        lambda tag: True,
    )

    svc = ProtonService(MagicMock())
    svc._emit_proton_toast = AsyncMock()

    await svc._ensure_latest_ge()

    # Outdated toast must be emitted with the new version
    svc._emit_proton_toast.assert_awaited_once_with(
        "toasts.launcher.externalProtonOutdatedTitle",
        "toasts.launcher.externalProtonOutdatedBody",
        "GE-Proton11-5",
    )


async def test_proton_service_external_ge_up_to_date_silent(monkeypatch):
    from unifideck.launcher.proton.infrastructure import external_ge
    from unifideck.services.proton_service import ProtonService

    fake_proton = Path("/fake/Proton-GE Latest/proton")
    monkeypatch.setattr(
        external_ge, "find_external_ge_proton",
        lambda roots=None: (fake_proton, "Proton-GE Latest", "GE-Proton11-5"),
    )
    monkeypatch.setattr(
        ge_installer, "get_latest_ge_tag",
        lambda timeout=8.0: "GE-Proton11-5",
    )
    monkeypatch.setattr(
        ge_installer, "is_valid_ge_install",
        lambda tag: True,
    )

    svc = ProtonService(MagicMock())
    svc._emit_proton_toast = AsyncMock()

    await svc._ensure_latest_ge()

    # Up to date: no toast emitted
    svc._emit_proton_toast.assert_not_awaited()


async def test_proton_service_external_ge_fresh_enough_skips_download(monkeypatch):
    """When external GE has a small minor lag (<=5), toast is emitted but download is skipped."""
    from unifideck.launcher.proton.infrastructure import external_ge
    from unifideck.services.proton_service import ProtonService

    fake_proton = Path("/fake/Proton-GE Latest/proton")
    monkeypatch.setattr(
        external_ge, "find_external_ge_proton",
        lambda roots=None: (fake_proton, "Proton-GE Latest", "GE-Proton11-3"),
    )
    monkeypatch.setattr(
        ge_installer, "get_latest_ge_tag",
        lambda timeout=8.0: "GE-Proton11-5",
    )
    is_valid_mock = MagicMock()
    monkeypatch.setattr(ge_installer, "is_valid_ge_install", is_valid_mock)
    ensure_mock = MagicMock()
    monkeypatch.setattr(ge_installer, "ensure_latest_ge", ensure_mock)

    svc = ProtonService(MagicMock())
    svc._emit_proton_toast = AsyncMock()

    await svc._ensure_latest_ge()

    # Outdated toast should still be emitted to notify user
    svc._emit_proton_toast.assert_awaited_once_with(
        "toasts.launcher.externalProtonOutdatedTitle",
        "toasts.launcher.externalProtonOutdatedBody",
        "GE-Proton11-5",
    )
    # But because it is sufficiently fresh (lag=2 <= 5), download check and execution are skipped
    is_valid_mock.assert_not_called()
    ensure_mock.assert_not_called()


async def test_proton_service_external_ge_severely_outdated_triggers_download(monkeypatch):
    """When external GE has a major lag, download proceeds to establish a modern recovery floor."""
    from unifideck.launcher.proton.infrastructure import external_ge
    from unifideck.services.proton_service import ProtonService

    fake_proton = Path("/fake/Proton-GE Latest/proton")
    fake_downloaded = Path("/fake/GE-Proton11-5/proton")
    monkeypatch.setattr(
        external_ge, "find_external_ge_proton",
        lambda roots=None: (fake_proton, "Proton-GE Latest", "GE-Proton10-34"),
    )
    monkeypatch.setattr(
        ge_installer, "get_latest_ge_tag",
        lambda timeout=8.0: "GE-Proton11-5",
    )
    monkeypatch.setattr(
        ge_installer, "is_valid_ge_install",
        lambda tag: False,
    )
    ensure_mock = MagicMock(return_value=(fake_downloaded, "GE-Proton11-5"))
    monkeypatch.setattr(ge_installer, "ensure_latest_ge", ensure_mock)

    svc = ProtonService(MagicMock())
    svc._emit_proton_toast = AsyncMock()

    await svc._ensure_latest_ge()

    # Must call ensure_latest_ge because major version differs (10 vs 11)
    ensure_mock.assert_called_once()
    toast_keys = [call.args[0] for call in svc._emit_proton_toast.await_args_list]
    assert "toasts.launcher.externalProtonOutdatedTitle" in toast_keys
    assert "toasts.launcher.installingProton" in toast_keys
    assert "toasts.launcher.protonReadyTitle" in toast_keys
