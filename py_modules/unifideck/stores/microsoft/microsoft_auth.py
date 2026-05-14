"""Low-level Microsoft auth primitives — HTTP helpers, XBL/XSTS token chain construction."""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import cast
from ...core.net import ssl_ctx_strict
logger = logging.getLogger(__name__)
__all__ = [
    "build_xbl_chain",
    "http_get",
    "http_post",
    "request_xsts_token",
    "ssl_ctx_strict",
]
def http_post(url: str, data: dict, headers: dict) -> dict:
    """POST URL-encoded form data and parse the JSON response.

    Uses the strict SSL context and a 15s timeout.

    Args:
        url: Target URL.
        data: Form fields (will be ``urlencode``-d).
        headers: Request headers (must include the right
            Content-Type for form data).

    Returns:
        Parsed JSON dict.
    """
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15, context=ssl_ctx_strict()) as r:
        return cast(dict, json.loads(r.read().decode()))
def http_post_json(url: str, payload: dict, headers: dict) -> dict:
    """POST a JSON body and parse the JSON response.

    Uses the strict SSL context and a 20s timeout.

    Args:
        url: Target URL.
        payload: Body dict (will be ``json.dumps``-d).
        headers: Request headers.

    Returns:
        Parsed JSON dict.
    """
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20, context=ssl_ctx_strict()) as r:
        return cast(dict, json.loads(r.read().decode()))
def http_get(url: str, headers: dict) -> dict:
    """GET a URL and parse the JSON response.

    Uses the strict SSL context and a 15s timeout.

    Args:
        url: Target URL.
        headers: Request headers.

    Returns:
        Parsed JSON dict.
    """
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15, context=ssl_ctx_strict()) as r:
        return cast(dict, json.loads(r.read().decode()))

def build_xbl_chain(
    access_token: str,
    locale: str,
    xbl_auth_url: str,
    xsts_url: str,
    xbl_user_agent: str,
    xsts_relying_party: str = "http://xboxlive.com",
) -> dict[str, str] | None:

    """Build the full XBL → XSTS token chain from a Microsoft access token.

    Steps: obtain an XBL user token (trying several
    contract-version + RpsTicket prefix combinations to
    absorb Microsoft's flaky auth surface), extract the
    user hash, then trade it for an XSTS token bound to
    the requested relying party.

    Args:
        access_token: OAuth access token from Microsoft.
        locale: BCP-47 locale (for ``Accept-Language``).
        xbl_auth_url: Xbox Live auth endpoint URL.
        xsts_url: XSTS endpoint URL.
        xbl_user_agent: User-Agent header value.
        xsts_relying_party: Relying-party URL
            (default ``http://xboxlive.com``; use
            ``http://gssv.xboxlive.com/`` for cloud-gaming).

    Returns:
        Dict ``{xbl_token, user_hash, xsts_token, xsts_rp,
        xuid}``, or ``None`` if any step in the chain failed.
    """
    logger.info("[MS] Building XBL/XSTS token chain")
    try:
        xbl_resp = _obtain_xbl_user_token(
            access_token, locale, xbl_auth_url, xbl_user_agent,
        )
        if xbl_resp is None:
            return None
        xbl_token = xbl_resp["Token"]
        user_hash = _extract_user_hash(xbl_resp)
        logger.info(
            "[MS] ✓ XBL user token obtained (uhs=%s)", user_hash,
        )
        xsts_rp = xsts_relying_party
        xsts_resp = _request_xsts_token(
            xbl_token, xsts_rp, locale, xsts_url, xbl_user_agent,
        )
        if xsts_resp is None:
            return None
        if "XErr" in xsts_resp:
            _log_xsts_xerr(xsts_resp["XErr"])
            return None
        xsts_token = xsts_resp.get("Token")
        if not xsts_token:
            logger.error(
                "[MS] XSTS token missing: %s", xsts_resp,
            )
            return None
        xsts_claims = xsts_resp.get(
            "DisplayClaims", {},
        ).get("xui", [{}])
        xuid = (
            xsts_claims[0].get("xid") if xsts_claims else None
        )
        logger.info(
            "[MS] ✓ XSTS token obtained (xuid=%s)", xuid,
        )
        return {
            "xbl_token": xbl_token,
            "user_hash": user_hash,
            "xsts_token": xsts_token,
            "xsts_rp": xsts_rp,
            "xuid": xuid,
        }
    except Exception as e:
        logger.exception(
            "[MS] XBL chain error: %s", e,
        )
        return None
def request_xsts_token(
    xbl_token: str,
    xsts_rp: str,
    locale: str,
    xsts_url: str,
    xbl_user_agent: str,
) -> dict | None:
    """Public wrapper that delegates to ``_request_xsts_token``.

    Args:
        xbl_token: XBL user token from ``_obtain_xbl_user_token``.
        xsts_rp: Relying-party URL.
        locale: BCP-47 locale.
        xsts_url: XSTS endpoint URL.
        xbl_user_agent: User-Agent header value.

    Returns:
        Parsed XSTS response dict, or ``None`` on failure.
    """
    return _request_xsts_token(
        xbl_token, xsts_rp, locale, xsts_url, xbl_user_agent,
    )

def _obtain_xbl_user_token(
    access_token: str,
    locale: str,
    xbl_auth_url: str,
    xbl_user_agent: str,
) -> dict | None:

    """Try several contract-version / RpsTicket prefix combos until one succeeds.

    Microsoft's XBL endpoint is sensitive to subtle
    differences between contract version (1 vs 2) and the
    RpsTicket prefix (``t=`` vs ``d=``). Tries all three
    known-working combos in order and returns the first
    non-empty response.

    Args:
        access_token: OAuth access token.
        locale: BCP-47 locale.
        xbl_auth_url: Xbox Live auth endpoint URL.
        xbl_user_agent: User-Agent header value.

    Returns:
        Parsed XBL response dict, or ``None`` if all combos
        failed.
    """
    candidates = [
        ("2", f"t={access_token}"),
        ("1", f"d={access_token}"),
        ("1", f"t={access_token}"),
    ]
    for contract_v, rps in candidates:
        resp = _try_xbl_request(
            contract_v, rps, locale, xbl_auth_url, xbl_user_agent,
        )
        if resp is not None and resp.get("Token"):
            logger.info(
                "[MS] XBL auth OK (contract-v%s, prefix=%r)",
                contract_v, rps[:2],
            )
            return resp
    logger.error(
        "[MS] XBL user token failed with all contract/prefix combos",
    )
    return None
def _try_xbl_request(
    contract_v: str,
    rps: str,
    locale: str,
    xbl_auth_url: str,
    xbl_user_agent: str,
) -> dict | None:
    """Run one XBL auth POST with a specific contract version + RpsTicket prefix.

    Args:
        contract_v: XBL contract version (``"1"`` or ``"2"``).
        rps: RpsTicket header (``t=...`` or ``d=...``).
        locale: BCP-47 locale.
        xbl_auth_url: Xbox Live auth endpoint URL.
        xbl_user_agent: User-Agent header value.

    Returns:
        Parsed response dict on HTTP 200, or ``None`` on
        any error (HTTP error body is logged at DEBUG).
    """
    body = {
        "Properties": {
            "AuthMethod": "RPS",
            "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": rps,
        },
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-xbl-contract-version": contract_v,
        "User-Agent": xbl_user_agent,
        "Accept-Language": locale,
    }
    try:
        return http_post_json(xbl_auth_url, body, headers)
    except urllib.error.HTTPError as e:
        body_text = _read_http_error_body(e)
        logger.debug(
            "[MS] XBL failed (v%s, %r): HTTP %d %s",
            contract_v, rps[:2], e.code, body_text[:500],
        )
        return None
    except Exception as e:
        logger.debug(
            "[MS] XBL failed (v%s, %r): %s",
            contract_v, rps[:2], e,
        )
        return None
def _extract_user_hash(xbl_resp: dict) -> str | None:
    """Pull the user hash out of an XBL response's DisplayClaims.

    Args:
        xbl_resp: Parsed XBL response.

    Returns:
        The ``uhs`` value, or ``None`` if absent.
    """
    display_claims = xbl_resp.get("DisplayClaims", {})
    xui = display_claims.get("xui", [{}])
    return xui[0].get("uhs") if xui else None

def _request_xsts_token(
    xbl_token: str,
    xsts_rp: str,
    locale: str,
    xsts_url: str,
    xbl_user_agent: str,
) -> dict | None:

    """POST to the XSTS endpoint to exchange an XBL token for an XSTS token.

    Args:
        xbl_token: XBL user token.
        xsts_rp: Relying party URL.
        locale: BCP-47 locale.
        xsts_url: XSTS endpoint URL.
        xbl_user_agent: User-Agent header value.

    Returns:
        Parsed XSTS response, or ``None`` on HTTP error /
        network failure.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-xbl-contract-version": "1",
        "User-Agent": xbl_user_agent,
        "Accept-Language": locale,
    }
    body = {
        "Properties": {
            "SandboxId": "RETAIL",
            "UserTokens": [xbl_token],
        },
        "RelyingParty": xsts_rp,
        "TokenType": "JWT",
    }
    try:
        resp = http_post_json(xsts_url, body, headers)
        logger.info(
            "[MS] ✓ XSTS obtained with RP=%r sandbox='RETAIL'",
            xsts_rp,
        )
        return resp
    except urllib.error.HTTPError as e:
        body_text = _read_http_error_body(e)
        logger.warning(
            "[MS] XSTS failed (RP=%r): HTTP %d %s",
            xsts_rp, e.code, body_text[:500],
        )
        return None
    except Exception as e:
        logger.warning(
            "[MS] XSTS failed (RP=%r): %s", xsts_rp, e,
        )
        return None
def _log_xsts_xerr(xerr: int) -> None:
    """Log a human-readable explanation for an XSTS ``XErr`` code.

    Recognized: ``2148916238`` (no Xbox profile),
    ``2148916233`` (country unsupported).

    Args:
        xerr: XSTS error code from the response.
    """
    logger.error("[MS] XSTS error code: %d", xerr)
    if xerr == 2148916238:
        logger.error(
            "[MS] Account has no Xbox profile — create one at xbox.com",
        )
    elif xerr == 2148916233:
        logger.error(
            "[MS] Account is from a country where Xbox is not available",
        )
def _read_http_error_body(err: urllib.error.HTTPError) -> str:
    """Best-effort read of an HTTPError's body.

    Args:
        err: The HTTPError instance.

    Returns:
        Decoded body text, or an empty string on read failure.
    """
    try:
        return err.read().decode("utf-8", errors="replace")
    except Exception:
        return ""