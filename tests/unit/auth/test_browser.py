"""Deep executable tests — auth/browser.py.

Source : py_modules/unifideck/auth/browser.py
Fiche  : OP-02a   Critical — coverage floor 95%.

Pure functions (extract_oauth_params / match_redirect) are
exercised with concrete inputs. The OAuthBrowserMonitor is
driven by a controllable FakeCDPClient so every async path
(redirect match, timeout, transient CDP error, tab close,
cookie clear incl. the security regex) is covered.
"""
from __future__ import annotations

import inspect
from typing import Any

import pytest

from unifideck.auth.browser import (
    AuthCaptureResult,
    CDPOAuthMonitor,
    OAuthBrowserMonitor,
    extract_oauth_params,
    match_redirect,
)


# --------------------------------------------------------- #
# Fake CDP client
# --------------------------------------------------------- #
class _FakeCDP:
    def __init__(
        self,
        targets: list[dict[str, Any]] | None = None,
        *,
        list_raises: bool = False,
        close_raises: bool = False,
        eval_raises: bool = False,
    ) -> None:
        self._targets = targets or []
        self._list_raises = list_raises
        self._close_raises = close_raises
        self._eval_raises = eval_raises
        self.closed: list[str] = []
        self.eval_calls: list[str] = []

    async def list_targets(self) -> list[dict[str, Any]]:
        if self._list_raises:
            raise RuntimeError("cdp down")
        return self._targets

    async def close_target(self, tid: str) -> None:
        if self._close_raises:
            raise RuntimeError("close failed")
        self.closed.append(tid)

    async def eval_js(self, js: str) -> Any:
        if self._eval_raises:
            raise RuntimeError("eval failed")
        self.eval_calls.append(js)
        return None


def _fast_monitor(cdp: _FakeCDP) -> OAuthBrowserMonitor:
    """Monitor with a tiny poll interval / timeout so the
    timeout path resolves fast in tests."""
    m = OAuthBrowserMonitor(cdp)  # type: ignore[arg-type]
    m._poll_interval = 0.001
    m._default_timeout = 0.02
    return m


def test_module_imports() -> None:
    import unifideck.auth.browser as mod
    assert mod.extract_oauth_params is extract_oauth_params


# --------------------------------------------------------- #
# extract_oauth_params (pure)
# --------------------------------------------------------- #
def test_extract_oauth_params_query() -> None:
    out = extract_oauth_params(
        "https://x.test/cb?code=ABC&state=xyz")
    assert out.get("code") == "ABC"
    assert out.get("state") == "xyz"


def test_extract_oauth_params_fragment() -> None:
    out = extract_oauth_params(
        "https://x.test/cb#access_token=TOK&token_type=b")
    assert isinstance(out, dict)


def test_extract_oauth_params_empty() -> None:
    assert extract_oauth_params("https://x.test/plain") \
        == {}


def test_extract_oauth_params_garbage() -> None:
    assert isinstance(
        extract_oauth_params("not-a-url"), dict)


# --------------------------------------------------------- #
# match_redirect (pure)
# --------------------------------------------------------- #
def test_match_redirect_true() -> None:
    assert match_redirect(
        "https://app.test/cb?code=1",
        ["https://app.test/cb"]) is True


def test_match_redirect_false() -> None:
    assert match_redirect(
        "https://evil.test/cb",
        ["https://app.test/cb"]) is False


def test_match_redirect_empty_allowed() -> None:
    assert match_redirect(
        "https://app.test/cb", []) is False


# --------------------------------------------------------- #
# AuthCaptureResult (value object)
# --------------------------------------------------------- #
def test_auth_capture_result_to_dict() -> None:
    r = AuthCaptureResult(success=True,
                           redirect_url="https://x/cb",
                           params={"code": "C", "state": "S"})
    d = r.to_dict()
    assert d["success"] is True
    assert d["redirect_url"] == "https://x/cb"


def test_auth_capture_result_code_state_props() -> None:
    r = AuthCaptureResult(
        success=True, params={"code": "C", "state": "S"})
    assert r.code == "C"
    assert r.state == "S"


def test_auth_capture_result_missing_code_is_none() -> None:
    r = AuthCaptureResult(success=False)
    assert r.code is None
    assert r.state is None


# --------------------------------------------------------- #
# CDPOAuthMonitor is an alias of OAuthBrowserMonitor
# --------------------------------------------------------- #
def test_cdp_monitor_is_alias() -> None:
    assert CDPOAuthMonitor is OAuthBrowserMonitor


# --------------------------------------------------------- #
# wait_for_redirect
# --------------------------------------------------------- #
@pytest.mark.asyncio
async def test_wait_for_redirect_matches() -> None:
    """A tab already on an allowed URI -> immediate success
    with extracted params."""
    cdp = _FakeCDP([
        {"id": "t1",
         "url": "https://app.test/cb?code=XYZ&state=s1"},
    ])
    m = _fast_monitor(cdp)
    res = await m.wait_for_redirect(
        ["https://app.test/cb"])
    assert res.success is True
    assert res.params.get("code") == "XYZ"
    assert res.redirect_url.startswith("https://app.test")


@pytest.mark.asyncio
async def test_wait_for_redirect_timeout() -> None:
    """No matching tab -> timeout failure result."""
    cdp = _FakeCDP([
        {"id": "t1", "url": "https://other.test/home"},
    ])
    m = _fast_monitor(cdp)
    res = await m.wait_for_redirect(
        ["https://app.test/cb"])
    assert res.success is False
    assert res.error == "timeout"


@pytest.mark.asyncio
async def test_wait_for_redirect_transient_cdp_error() -> None:
    """A CDP list error must not abort capture; it retries
    then times out gracefully."""
    cdp = _FakeCDP(list_raises=True)
    m = _fast_monitor(cdp)
    res = await m.wait_for_redirect(
        ["https://app.test/cb"])
    assert res.success is False


# --------------------------------------------------------- #
# close_oauth_tab
# --------------------------------------------------------- #
@pytest.mark.asyncio
async def test_close_oauth_tab_success() -> None:
    cdp = _FakeCDP([
        {"id": "t9", "url": "https://login.x/oauth"},
    ])
    m = _fast_monitor(cdp)
    assert await m.close_oauth_tab("oauth") is True
    assert cdp.closed == ["t9"]


@pytest.mark.asyncio
async def test_close_oauth_tab_not_found() -> None:
    cdp = _FakeCDP([{"id": "t1", "url": "https://x/home"}])
    m = _fast_monitor(cdp)
    assert await m.close_oauth_tab("oauth") is False


@pytest.mark.asyncio
async def test_close_oauth_tab_list_error() -> None:
    cdp = _FakeCDP(list_raises=True)
    m = _fast_monitor(cdp)
    assert await m.close_oauth_tab("oauth") is False


@pytest.mark.asyncio
async def test_close_oauth_tab_close_error() -> None:
    """A close failure -> False (logged, non-fatal)."""
    cdp = _FakeCDP(
        [{"id": "tX", "url": "https://login.x/oauth"}],
        close_raises=True)
    m = _fast_monitor(cdp)
    assert await m.close_oauth_tab("oauth") is False


@pytest.mark.asyncio
async def test_close_oauth_tab_missing_id_skipped() -> None:
    """A matching tab without an id is skipped."""
    cdp = _FakeCDP([{"url": "https://login.x/oauth"}])
    m = _fast_monitor(cdp)
    assert await m.close_oauth_tab("oauth") is False


# --------------------------------------------------------- #
# clear_store_cookies — incl. the security regex
# --------------------------------------------------------- #
@pytest.mark.asyncio
async def test_clear_store_cookies_valid_domain() -> None:
    cdp = _FakeCDP()
    m = _fast_monitor(cdp)
    assert await m.clear_store_cookies(
        "login.live.com") is True
    assert cdp.eval_calls  # JS was injected


@pytest.mark.asyncio
async def test_clear_store_cookies_rejects_bad_domain(
) -> None:
    """A domain failing the strict regex is rejected without
    touching the CDP client (JS injection guard)."""
    cdp = _FakeCDP()
    m = _fast_monitor(cdp)
    assert await m.clear_store_cookies(
        "evil.com'; alert(1)//") is False
    assert cdp.eval_calls == []


@pytest.mark.asyncio
async def test_clear_store_cookies_eval_error() -> None:
    """An eval failure -> False (logged, non-fatal)."""
    cdp = _FakeCDP(eval_raises=True)
    m = _fast_monitor(cdp)
    assert await m.clear_store_cookies(
        "valid.com") is False


def test_monitor_methods_are_async() -> None:
    for n in ("wait_for_redirect", "close_oauth_tab",
              "clear_store_cookies"):
        assert inspect.iscoroutinefunction(
            getattr(OAuthBrowserMonitor, n))


# --- pure-function edge branches toward 95% ------------ #
def test_extract_oauth_params_empty_url_returns_empty(
) -> None:
    """The `if not url` guard -> {}."""
    assert extract_oauth_params("") == {}


def test_match_redirect_empty_url_false() -> None:
    """The `if not url` guard -> False."""
    assert match_redirect("", ["https://app.test/cb"]) \
        is False


def test_match_redirect_rejects_non_https_scheme() -> None:
    """A non-https (and non-localhost-http) callback is
    rejected for security."""
    assert match_redirect(
        "ftp://app.test/cb",
        ["ftp://app.test/cb"]) is False


def test_match_redirect_allows_http_localhost() -> None:
    """http is accepted only for localhost loopback
    callbacks."""
    assert match_redirect(
        "http://localhost/cb?code=1",
        ["http://localhost/cb"]) is True


def test_match_redirect_skips_empty_prefix() -> None:
    """An empty entry in allowed_uris is skipped, a later
    valid one still matches."""
    assert match_redirect(
        "https://app.test/cb",
        ["", "https://app.test/cb"]) is True


def test_legacy_cfg_alias_delegates() -> None:
    """The legacy ``_cfg`` alias was removed in the
    restructure; ``auth.browser`` now imports ``get_cfg``
    directly from ``utils.config_helpers``. Verify the
    underlying helper still honours the default-on-miss
    contract the alias used to provide."""
    from unifideck.utils.config_helpers import get_cfg
    # unknown key with config=None -> returns the default
    assert get_cfg(None, "auth.does_not_exist", 42) == 42


# --- wait_for_redirect: transient error inside the loop - #
@pytest.mark.asyncio
async def test_wait_for_redirect_inner_list_error_retries(
    monkeypatch,
) -> None:
    """If _list_targets raises *inside* the polling loop, the
    monitor logs, sleeps and retries (the except/continue
    branch), then times out."""
    cdp = _FakeCDP()
    m = _fast_monitor(cdp)

    async def _boom() -> Any:
        raise RuntimeError("transient cdp")

    monkeypatch.setattr(m, "_list_targets", _boom)
    res = await m.wait_for_redirect(
        ["https://app.test/cb"])
    assert res.success is False
    assert res.error == "timeout"


@pytest.mark.asyncio
async def test_list_targets_swallows_error_returns_empty(
) -> None:
    """The private _list_targets wrapper degrades a CDP error
    to an empty list (not a raise)."""
    cdp = _FakeCDP(list_raises=True)
    m = _fast_monitor(cdp)
    out = await m._list_targets()
    assert out == []
