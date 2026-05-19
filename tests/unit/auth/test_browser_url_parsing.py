"""Deep behavioural tests — auth/browser_url_parsing.py.

Source : py_modules/unifideck/auth/browser_url_parsing.py
Pure URL-parsing layer of the OAuth browser monitor: OAuth
param extraction, redirect-prefix matching, relevance gate,
capture-result builders, the legacy ``_cfg`` alias, and the
multi-provider ``extract_code_from_url`` (standard ``code=``,
Epic ``authorizationCode=``, Amazon
``openid.oa2.authorization_code=``).
"""
from __future__ import annotations

import pytest

from unifideck.auth.browser_url_parsing import (
    _cfg,
    build_redirect_capture,
    build_url_code_capture,
    extract_code_from_url,
    extract_oauth_params,
    is_oauth_relevant_url,
    match_redirect,
)


# ========================================================= #
# extract_oauth_params
# ========================================================= #
def test_extract_params_basic() -> None:
    out = extract_oauth_params(
        "https://cb/x?code=ABC&state=42")
    assert out["code"] == "ABC"
    assert out["state"] == "42"


def test_extract_params_none() -> None:
    assert extract_oauth_params(
        "https://cb/x") == {}


# ========================================================= #
# match_redirect
# ========================================================= #
def test_match_redirect_true() -> None:
    assert match_redirect(
        "https://cb/done?code=1",
        ["https://cb/done"]) is True


def test_match_redirect_false() -> None:
    assert match_redirect(
        "https://other/x",
        ["https://cb/done"]) is False


# ========================================================= #
# _cfg legacy alias
# ========================================================= #
def test_cfg_alias_delegates_default() -> None:
    # config=None -> returns default (delegates to get_cfg)
    assert _cfg(None, "auth.missing", 99) == 99


# ========================================================= #
# is_oauth_relevant_url
# ========================================================= #
@pytest.mark.parametrize("url", [
    "https://x/oauth/authorize?client_id=1",
    "https://x/login?redirect=1",
    "https://x/cb?code=abc",
])
def test_is_relevant_true(url) -> None:
    assert is_oauth_relevant_url(url) is True


def test_is_relevant_false() -> None:
    assert is_oauth_relevant_url(
        "https://example.com/about") is False


def test_is_relevant_empty() -> None:
    assert is_oauth_relevant_url("") is False


# ========================================================= #
# build_redirect_capture / build_url_code_capture
# ========================================================= #
def test_build_redirect_capture() -> None:
    res = build_redirect_capture(
        "https://cb/done?code=Z", 0.0)
    assert res.success is True
    assert res.redirect_url == \
        "https://cb/done?code=Z"
    assert res.elapsed_seconds >= 0


def test_build_url_code_capture() -> None:
    res = build_url_code_capture(
        "https://cb/x", "CODE-42", 0.0)
    assert res.success is True
    assert res.params["code"] == "CODE-42"
    assert res.redirect_url == "https://cb/x"
    assert res.elapsed_seconds >= 0


# ========================================================= #
# extract_code_from_url — all provider branches
# ========================================================= #
def test_extract_code_empty() -> None:
    assert extract_code_from_url("") is None


def test_extract_code_standard() -> None:
    assert extract_code_from_url(
        "https://cb/x?code=STD123&state=1") \
        == "STD123"


def test_extract_code_epic() -> None:
    assert extract_code_from_url(
        "https://epic/redirect?"
        "authorizationCode=EPIC-9&x=1") == "EPIC-9"


def test_extract_code_amazon() -> None:
    assert extract_code_from_url(
        "https://amazon/ap?"
        "openid.oa2.authorization_code=AMZ-7&y=2") \
        == "AMZ-7"


def test_extract_code_amazon_lowercase_param(
) -> None:
    # The presence check is case-insensitive (url.lower())
    # but the capture regex matches the canonical lowercase
    # param name, which is how Amazon actually sends it.
    assert extract_code_from_url(
        "https://amazon/ap?"
        "openid.oa2.authorization_code=AMZ-CI") \
        == "AMZ-CI"


def test_extract_code_none_present() -> None:
    assert extract_code_from_url(
        "https://cb/x?state=only") is None


def test_extract_code_empty_code_param() -> None:
    # "code=" present but no value -> None
    assert extract_code_from_url(
        "https://cb/x?code=&state=1") is None


def test_extract_code_epic_priority() -> None:
    # Epic pattern checked before standard code=
    out = extract_code_from_url(
        "https://x?authorizationCode=EP&code=STD")
    assert out == "EP"
