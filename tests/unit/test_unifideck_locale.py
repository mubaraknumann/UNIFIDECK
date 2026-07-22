"""Locale resolution: ui.locale preference drives backend locale."""

from __future__ import annotations

from typing import Any

import pytest

from unifideck.utils import locale as locale_mod

# Arbitrary BCP-47 tags for assertions — not a product default.
_SAVED_TAG = "ja-JP"
_SYSTEM_TAG = "it-IT"


class _StubConfig:
    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)


def test_ui_locale_preference_is_used() -> None:
    cfg = _StubConfig({"ui.locale": _SAVED_TAG})
    assert locale_mod.get_unifideck_locale(cfg) == _SAVED_TAG


def test_ui_locale_auto_falls_through_to_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        locale_mod,
        "_detect_system_locale",
        lambda _lc: _SYSTEM_TAG,
    )
    cfg = _StubConfig({"ui.locale": "auto"})
    assert locale_mod.get_unifideck_locale(cfg) == _SYSTEM_TAG


def test_locale_to_epic_language() -> None:
    assert locale_mod.locale_to_epic_language(_SAVED_TAG) == "ja"
    assert locale_mod.locale_to_epic_language("en-US") == "en"
