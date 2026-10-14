"""Unit tests for the Luna launch-args builder (task 4.2, Q1 abstraction).

Exercises ``LunaBrowser`` as a *pure config/composition* object over the
reused ``EdgeBrowser``: the kiosk arg composition (``build_kiosk_args``),
the Q1 browser-command resolution (``resolve_browser_cmd`` — Chrome and
Edge candidates, not hardcoded Edge), and the Req 6.2 abort when the
gamepad-flag set is missing or malformed.

No real browser is spawned: an ``EdgeBrowser`` is injected with a fixed
``cdp_port`` + ``locale_fn`` and a deterministic ``env`` is passed so the
display-derived window flags are stable.
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.auth.edge_browser.edge import (
    _BASE_FLAGS,
    WEB_GAME_PROFILE_DIR,
    EdgeBrowser,
)
from unifideck.stores.luna import luna_browser as mod
from unifideck.stores.luna.luna_browser import (
    LunaBrowser,
    LunaGamepadFlagError,
    resolve_browser_cmd,
    validate_gamepad_flags,
)
from unifideck.stores.luna.luna_config import LunaConfig

_DETAIL_URL = "https://luna.amazon.de/game/amzn1.adg.product.abc-123"
_GAMEPAD_FLAGS = [
    "--enable-features=WebGamepadButtonAxisEvents",
    "--gamepad-polling-rate=250",
]
_CLOUD_FLAGS = ["--enable-features=VaapiVideoDecoder"]
#: A deterministic env so ``auth_window_flags`` (reads screen size from env)
#: produces a stable window-flag set across machines.
_ENV = {"DISPLAY": ":0", "XAUTHORITY": "/run/user/1000/xauth"}


def _config(
    *,
    profile_dir: str | None = "/home/deck/.local/share/unifideck/luna",
    candidates: list[str] | None = None,
    gamepad_flags: list[str] | None = None,
    cloud_flags: list[str] | None = None,
) -> LunaConfig:
    return LunaConfig(
        luna_host="luna.amazon.de",
        allowed_stream_hosts=["luna.amazon.de"],
        profile_dir=profile_dir,
        browser_candidates=candidates if candidates is not None else ["com.google.Chrome"],
        gamepad_flags=gamepad_flags if gamepad_flags is not None else list(_GAMEPAD_FLAGS),
        cloud_browser_flags=cloud_flags if cloud_flags is not None else list(_CLOUD_FLAGS),
    )


def _browser(config: LunaConfig, *, cdp_port: int = 9222, locale: str = "en-US") -> LunaBrowser:
    edge = EdgeBrowser(cdp_port=cdp_port, locale_fn=lambda: locale)
    return LunaBrowser(config, edge=edge)


# ── build_kiosk_args: well-formed kiosk set ──────────────────────────


@pytest.mark.parametrize("cmd", [
    ["google-chrome"],
    ["microsoft-edge"],
    ["flatpak", "run", "com.google.Chrome"],
], ids=["chrome-native", "edge-native", "chrome-flatpak"])
def test_build_kiosk_args_well_formed_for_any_browser_cmd(cmd: list[str]) -> None:
    br = _browser(_config())
    args = br.build_kiosk_args(cmd, _DETAIL_URL, env=_ENV)

    # Resolved browser command is prefixed verbatim (Q1: brand not hardcoded).
    assert args[: len(cmd)] == cmd
    # Kiosk mode + the Luna window class.
    assert "--kiosk" in args
    assert f"--class={mod._WINDOW_CLASS}" in args
    # CDP port is the browser-game port (offset from cdp_port), not 9224.
    assert f"--remote-debugging-port={br.cdp_port()}" in args
    assert "--remote-debugging-port=9224" not in args
    # Dedicated Luna profile dir.
    assert f"--user-data-dir={br.profile_dir()}" in args
    # Shared base flags, configured cloud + gamepad flags, autoplay relaxation.
    for flag in _BASE_FLAGS:
        assert flag in args
    for flag in _CLOUD_FLAGS:
        assert flag in args
    for flag in _GAMEPAD_FLAGS:
        assert flag in args
    assert mod._AUTOPLAY_FLAG in args
    # Locale and URL last.
    assert "--lang=en" in args
    assert args[-1] == _DETAIL_URL


def test_cdp_port_is_browser_game_offset_not_bespoke() -> None:
    br = _browser(_config(), cdp_port=9222)
    # browser_game_cdp_port() is an offset from cdp_port; assert they track
    # together rather than hardcoding a magic number.
    assert br.cdp_port() == EdgeBrowser(cdp_port=9222).browser_game_cdp_port()
    assert br.cdp_port() != 9222
    assert _browser(_config(), cdp_port=9300).cdp_port() == \
        EdgeBrowser(cdp_port=9300).browser_game_cdp_port()


def test_profile_dir_defaults_to_web_game_profile_when_unset() -> None:
    br = _browser(_config(profile_dir=None))
    args = br.build_kiosk_args(["google-chrome"], _DETAIL_URL, env=_ENV)
    assert br.profile_dir() == WEB_GAME_PROFILE_DIR
    assert f"--user-data-dir={WEB_GAME_PROFILE_DIR}" in args


def test_configured_profile_dir_wins_over_default() -> None:
    custom = "/home/deck/.local/share/unifideck/luna"
    br = _browser(_config(profile_dir=custom))
    assert br.profile_dir() == custom
    args = br.build_kiosk_args(["google-chrome"], _DETAIL_URL, env=_ENV)
    assert f"--user-data-dir={custom}" in args


def test_lang_uses_language_part_of_locale() -> None:
    br = _browser(_config(), locale="fr-FR")
    args = br.build_kiosk_args(["google-chrome"], _DETAIL_URL, env=_ENV)
    assert "--lang=fr" in args


# ── Q1: browser-command resolution over configured candidates ────────


def test_resolve_browser_cmd_prefers_first_installed_native(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the second candidate (edge) resolves; the first (chrome) is absent.
    monkeypatch.setattr(mod.shutil, "which", lambda c: "/usr/bin/" + c if c == "microsoft-edge" else None)
    cmd = resolve_browser_cmd(["google-chrome", "microsoft-edge"])
    assert cmd == ["microsoft-edge"]


def test_resolve_browser_cmd_resolves_flatpak_app_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda c: "/usr/bin/flatpak" if c == "flatpak" else None)
    monkeypatch.setattr(
        mod, "_try_flatpak_app",
        lambda app_id, _env: ["flatpak", "run", app_id] if app_id == "com.google.Chrome" else None,
    )
    cmd = resolve_browser_cmd(["com.google.Chrome"])
    assert cmd == ["flatpak", "run", "com.google.Chrome"]


def test_resolve_browser_cmd_returns_none_when_nothing_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda _c: None)
    assert resolve_browser_cmd(["google-chrome", "microsoft-edge"]) is None


def test_find_cmd_uses_configured_candidates_not_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda c: "/usr/bin/" + c if c == "google-chrome" else None)

    def _fail() -> list[str] | None:
        raise AssertionError("must not fall back to EdgeBrowser.find_cmd when candidates set")

    br = _browser(_config(candidates=["google-chrome"]))
    monkeypatch.setattr(br.edge, "find_cmd", _fail)
    assert br.find_cmd() == ["google-chrome"]


def test_find_cmd_falls_back_to_edge_when_no_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    br = _browser(_config(candidates=[]))
    monkeypatch.setattr(br.edge, "find_cmd", lambda: ["microsoft-edge"])
    assert br.find_cmd() == ["microsoft-edge"]


# ── Req 6.2: missing / malformed gamepad flag aborts, names the flag ─


def test_build_kiosk_args_aborts_when_gamepad_flags_missing() -> None:
    br = _browser(_config(gamepad_flags=[]))
    with pytest.raises(LunaGamepadFlagError) as exc:
        br.build_kiosk_args(["google-chrome"], _DETAIL_URL, env=_ENV)
    assert exc.value.flag is None
    assert "gamepad_flags" in str(exc.value)


@pytest.mark.parametrize("bad", ["", "   ", "enable-gamepad", "-single-dash"])
def test_build_kiosk_args_aborts_on_malformed_gamepad_flag(bad: str) -> None:
    br = _browser(_config(gamepad_flags=["--valid-flag", bad]))
    with pytest.raises(LunaGamepadFlagError) as exc:
        br.build_kiosk_args(["google-chrome"], _DETAIL_URL, env=_ENV)
    # The offending flag is named in the raised error.
    assert exc.value.flag == bad


def test_validate_gamepad_flags_accepts_well_formed_set() -> None:
    # key=value and bare long flags are both well-formed; no raise.
    validate_gamepad_flags(["--enable-features=WebGamepad", "--gamepad-polling-rate=250"])


def test_validate_gamepad_flags_names_first_offender() -> None:
    with pytest.raises(LunaGamepadFlagError) as exc:
        validate_gamepad_flags(["--ok", "bad-one", "--also-ok"])
    assert exc.value.flag == "bad-one"


def _assert_no_browser_spawned(br: LunaBrowser, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: Any, **_k: Any) -> bool:
        raise AssertionError("browser must not be spawned when gamepad flags invalid")

    monkeypatch.setattr(mod._launch, "_spawn_edge_process", _boom)


def test_launch_does_not_spawn_when_gamepad_flags_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    br = _browser(_config(candidates=["google-chrome"], gamepad_flags=["bad"]))
    monkeypatch.setattr(mod.shutil, "which", lambda c: "/usr/bin/" + c if c == "google-chrome" else None)
    monkeypatch.setattr(br.edge, "kill", lambda: None)
    monkeypatch.setattr(br.edge, "cleanup_stale_profile_state", lambda: None)
    monkeypatch.setattr(mod.Path, "mkdir", lambda *a, **k: None)
    _assert_no_browser_spawned(br, monkeypatch)
    with pytest.raises(LunaGamepadFlagError):
        br.launch(_DETAIL_URL)
