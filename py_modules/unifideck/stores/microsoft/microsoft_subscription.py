"""Game Pass subscription probe — calls the GSSV login endpoint and classifies the tier (Essential / Premium / Ultimate)."""

from __future__ import annotations
import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from ...core.types import SubscriptionTier
from .microsoft_auth import ssl_ctx_strict
logger = logging.getLogger(__name__)
_PROBE_TIMEOUT_SECONDS = 10
_GSSV_CLIENT_HEADER = "XboxComBrowser"
@dataclass(frozen=True)
class SubscriptionProbeResult:
    """Outcome of one Game Pass subscription probe.

    Attributes:
        tier: Detected subscription tier (NONE,
            ESSENTIAL, PREMIUM, ULTIMATE, ACTIVE_UNKNOWN).
        ok: True iff the probe completed; False if the
            probe failed at the transport/protocol level
            (network error, bad JSON, …).
        error: Error code on failure (``network``, ``timeout``,
            ``bad_response``, ``http_error``); ``None`` on success.
        http_status: HTTP status code if a response was received.
    """
    tier: SubscriptionTier
    ok: bool
    error: str | None = None
    http_status: int | None = None
async def probe_subscription(
    user_hash: str,
    gssv_xsts_token: str,
    endpoint_url: str,
    timeout_seconds: int = _PROBE_TIMEOUT_SECONDS,
) -> SubscriptionProbeResult:
    """Probe the GSSV ``v2/login/user`` endpoint to detect the user's tier.

    Runs the synchronous probe in a thread executor so the
    event loop stays responsive.

    Args:
        user_hash: XBL user hash (``uhs``).
        gssv_xsts_token: XSTS token bound to the GSSV
            relying party.
        endpoint_url: GSSV login endpoint URL.
        timeout_seconds: Probe timeout.

    Returns:
        A ``SubscriptionProbeResult``.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: _probe_sync(
            user_hash, gssv_xsts_token, endpoint_url, timeout_seconds,
        ),
    )

def _probe_sync(
    user_hash: str,
    gssv_xsts_token: str,
    endpoint_url: str,
    timeout_seconds: int,
) -> SubscriptionProbeResult:

    """Synchronous implementation of the GSSV probe.

    POSTs an empty body with XBL3.0 auth headers; parses the
    JSON response and classifies the tier. HTTP 401/403/404
    are treated as ``tier=NONE, ok=True`` (the probe ran;
    the user just has no subscription).

    Args:
        user_hash: XBL user hash.
        gssv_xsts_token: GSSV XSTS token.
        endpoint_url: GSSV login endpoint URL.
        timeout_seconds: Probe timeout.

    Returns:
        A ``SubscriptionProbeResult``.
    """
    headers = {
        "Authorization": f"XBL3.0 x={user_hash};{gssv_xsts_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Cache-Control": "no-store, must-revalidate, no-cache",
        "x-gssv-client": _GSSV_CLIENT_HEADER,
    }
    req = urllib.request.Request(
        endpoint_url, data=b"", headers=headers, method="POST",
    )
    http_result = _do_probe_http(req, timeout_seconds, endpoint_url)
    if isinstance(http_result, SubscriptionProbeResult):
        return http_result
    status, raw = http_result
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        logger.warning(
            "[SubscriptionProbe] response is not JSON: %s", e,
        )
        return SubscriptionProbeResult(
            tier=SubscriptionTier.NONE,
            ok=False,
            error="bad_response",
            http_status=status,
        )
    tier = _parse_tier_from_response(parsed)
    logger.info(
        "[SubscriptionProbe] endpoint %s responded 200, tier=%s",
        endpoint_url, tier.value,
    )
    return SubscriptionProbeResult(
        tier=tier,
        ok=True,
        http_status=status,
    )

def _do_probe_http(
    req: urllib.request.Request,
    timeout_seconds: int,
    endpoint_url: str,
) -> tuple[int, str] | SubscriptionProbeResult:

    """Run the HTTP request and return either ``(status, body)`` or a Result.

    Encapsulates the urllib error handling so the caller
    stays linear. 401/403/404 short-circuits into a
    ready-to-return NONE result (the probe ran but the user
    isn't subscribed); all other errors return a NONE result
    with the appropriate error code.

    Args:
        req: Prepared request object.
        timeout_seconds: Probe timeout.
        endpoint_url: For diagnostic logging.

    Returns:
        Either ``(status, body_text)`` on a successful HTTP
        fetch, or a fully-formed ``SubscriptionProbeResult``
        on any error/short-circuit.
    """
    try:
        with urllib.request.urlopen(
            req,
            timeout=timeout_seconds,
            context=ssl_ctx_strict(),
        ) as resp:
            return resp.status, resp.read().decode(
                "utf-8", errors="replace",
            )
    except urllib.error.HTTPError as e:
        status = e.code
        if status in (401, 403, 404):
            return SubscriptionProbeResult(
                tier=SubscriptionTier.NONE,
                ok=True,
                http_status=status,
            )
        logger.warning(
            "[SubscriptionProbe] unexpected HTTP %d on %s",
            status, endpoint_url,
        )
        return SubscriptionProbeResult(
            tier=SubscriptionTier.NONE,
            ok=False,
            error="http_error",
            http_status=status,
        )
    except (TimeoutError, urllib.error.URLError) as e:
        logger.warning(
            "[SubscriptionProbe] network error: %s", e,
        )
        return SubscriptionProbeResult(
            tier=SubscriptionTier.NONE,
            ok=False,
            error="timeout" if isinstance(e, TimeoutError) else "network",
        )
    except Exception as e:
        logger.warning(
            "[SubscriptionProbe] unexpected error: %s", e,
        )
        return SubscriptionProbeResult(
            tier=SubscriptionTier.NONE,
            ok=False,
            error="network",
        )
def _parse_tier_from_response(
    payload: dict[str, Any],
) -> SubscriptionTier:
    """Classify the GSSV response into a ``SubscriptionTier``.

    Requires ``offeringSettings.regions`` to be a non-empty
    list (no regions = no subscription). Tries the
    ``subscriptionTier``/``tier``/``offeringId`` fields at
    the top level and inside ``offeringSettings``.

    Args:
        payload: Parsed JSON response body.

    Returns:
        Tier — NONE if no regions or no recognized tier
        string; ACTIVE_UNKNOWN if regions exist but no
        field carries a tier string we recognize.
    """
    if not isinstance(payload, dict):
        return SubscriptionTier.NONE
    offering = payload.get("offeringSettings")
    if not isinstance(offering, dict):
        return SubscriptionTier.NONE
    regions = offering.get("regions")
    if not isinstance(regions, list) or len(regions) == 0:
        return SubscriptionTier.NONE
    for candidate_field in (
            "subscriptionTier", "tier", "offeringId"):
        value = payload.get(candidate_field)
        if value is None:
            value = offering.get(candidate_field)
        if isinstance(value, str):
            parsed = _match_tier_string(value)
            if parsed is not None:
                return parsed
    return SubscriptionTier.ACTIVE_UNKNOWN

def _match_tier_string(raw: str) -> SubscriptionTier | None:

    """Pattern-match a tier string against known tier markers.

    Detects ``ultimate`` / ``xgpu`` / ``xgpuweb…`` (with
    the F2P variant downgraded to NONE), ``premium``, and
    ``essential`` / ``core``.

    Args:
        raw: Tier string from the response.

    Returns:
        Matched tier, or ``None`` if unrecognized.
    """
    low = raw.lower()
    if "ultimate" in low or low == "xgpu" or low.startswith("xgpuweb"):
        if "f2p" in low:
            return SubscriptionTier.NONE
        return SubscriptionTier.ULTIMATE
    if "premium" in low:
        return SubscriptionTier.PREMIUM
    if "essential" in low or "core" in low:
        return SubscriptionTier.ESSENTIAL
    return None