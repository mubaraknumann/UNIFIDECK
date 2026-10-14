"""LunaPlatformSpoofer: browser-level CDP OS-identity override that defeats
Luna's Linux block for the whole streaming session (UA Client Hints + UA
string + navigator.platform + WebGL renderer).

This module currently implements the **version-tracking** core (task 5.1,
Req 14.8/14.9/12.6): the spoofed UA string and UA Client Hints metadata
must carry the major version of the *real installed browser* so Luna does
not reject the client as outdated (F4). A hardcoded version is never
emitted.

The runtime CDP fetch of ``/json/version`` and the pure parsing /
substitution are kept **separate** so the parsing is testable without a
browser: :func:`parse_browser_version` and :func:`build_spoof_identity`
are pure and deterministic; :func:`derive_browser_version` is the only
part that talks to a live browser over CDP.

The browser-level auto-attach and per-target override application are
implemented here (task 5.3, Req 14.1-14.7, 14.10). The I/O (the browser
websocket auto-attach loop) is kept **separate** from the pure
construction of the override payloads so the payload builders
(:func:`build_set_user_agent_override_params`,
:func:`build_platform_override_script`) can be unit/property tested
without a browser, and :meth:`LunaPlatformSpoofer.attach` can be driven
against a mock CDP session (:class:`BrowserCdpSession`) to assert, for any
set of targets, that the full override is applied to each **before** it is
resumed from waiting-for-debugger.

The ``verify()`` platform assertion is task 5.5 (Req 14.11, 14.12, 11.4):
after the detail page loads and before the play-click it evaluates
``navigator.userAgentData.getHighEntropyValues(['platform'])`` on the page
target and **returns** the reported platform; the launch flow (task 7.4)
decides the mismatch and maps it to ``platform_spoofing_failure``. The CDP
evaluate is one impure call (:meth:`LunaPlatformSpoofer.run_verify`) and the
extraction of the platform from the nested CDP result is a separate pure,
testable helper (:func:`extract_platform_from_evaluate`). No xCloud analogue
exists; 0.7.7 ships no spoofer.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from unifideck.stores.luna.luna_cdp import attach_to_page_target

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from unifideck.auth.edge_browser.cdp_client import EdgeCDPClient
    from unifideck.stores.luna.luna_config import LunaConfig

logger = logging.getLogger(__name__)

#: Placeholder in ``LunaConfig.spoof_ua_template`` substituted at runtime
#: with the real installed-browser major version (Req 12.6, 14.8).
_VERSION_PLACEHOLDER = "{version}"

#: A ``/json/version`` "Browser" field is ``<Product>/<major>.<minor>...``;
#: capture the product label and the leading numeric major version. Covers
#: ``Chrome/150.0.7871.186``, ``Edg/128.0.0.0``, ``HeadlessChrome/150.x``.
_BROWSER_FIELD_RE = re.compile(r"^(?P<product>[^/\s]+)/(?P<major>\d+)(?:\.\d+)*")


#: The CDP JSON-RPC error code returned when a method/domain does not exist
#: on a target (``-32601``, "method not found"). The ``Page`` domain is absent
#: on ``background_page``/``service_worker``/``worker``/``other`` targets, so
#: ``Page.enable`` on them fails with this code; the spoofer tolerates it and
#: applies the UA override only (the whole-session-aborting bug otherwise).
_CDP_METHOD_NOT_FOUND = -32601

#: Idle window for the auto-attach event loop (``attached_targets``).
#: ``Target.setAutoAttach`` delivers one ``attachedToTarget`` burst for the
#: open targets, then the browser websocket goes silent. The loop ends after
#: this long without a new target so ``run_attach`` returns instead of
#: blocking forever (the on-device catalog-scrape hang). Comfortably longer
#: than the sub-second gap between a burst's events, short enough to keep the
#: whole attach well inside the catalog timeout budget.
_ATTACH_IDLE_TIMEOUT_S = 2.0


def _is_domain_not_found(exc: Exception) -> bool:
    """Whether ``exc`` is a CDP "domain/method not found" error (``-32601``).

    :meth:`_AiohttpBrowserCdpSession.send` wraps a CDP error payload into a
    :class:`RuntimeError` whose text embeds the ``repr`` of the error dict
    (``{'code': -32601, 'message': "'Page.enable' wasn't found"}``). Match on
    the ``-32601`` code or the characteristic "wasn't found" message so the
    guard is robust to the exact string form, and does **not** match unrelated
    errors (only this specific not-found case is tolerated).
    """
    text = str(exc)
    return str(_CDP_METHOD_NOT_FOUND) in text or "wasn't found" in text


class LunaSpoofConfigError(Exception):
    """A spoofing-configuration failure that names the undetermined version.

    Raised when the real installed browser version cannot be derived or
    parsed (Req 14.9). The launcher maps this to the
    ``platform_spoofing_failure`` abort path (tasks 5.3 / 7.4): the Luna
    launch must not proceed with a hardcoded or missing version.

    ``reported`` carries the raw ``/json/version`` "Browser" value that
    could not be interpreted (or an empty string when nothing was
    reported) so the aborting log can name it.
    """

    def __init__(self, reported: str, message: str) -> None:
        """Build the error carrying the raw reported (undetermined) value."""
        super().__init__(message)
        self.reported = reported


@dataclass(frozen=True)
class SpoofIdentity:
    """The runtime-derived identity substituted into every target override.

    All version fields track the real installed browser (Req 14.8); none
    is hardcoded. ``major_version`` is the integer major parsed from the
    real browser; ``full_version`` is the full dotted version string as
    reported. ``user_agent`` is ``spoof_ua_template`` with its
    ``{version}`` placeholder substituted. ``ua_ch_full_version`` and
    ``ua_ch_major_version`` are the UA Client Hints metadata version
    fields (``fullVersionList`` entries / ``uaFullVersion`` and the brand
    ``version`` respectively).
    """

    major_version: int
    full_version: str
    user_agent: str
    ua_ch_major_version: str
    ua_ch_full_version: str


def parse_browser_version(browser_field: str | None) -> tuple[int, str]:
    """Parse a ``/json/version`` "Browser" field into (major, full_version).

    ``browser_field`` looks like ``"Chrome/150.0.7871.186"`` or
    ``"Edg/128.0.0.0"``. Returns the integer major version and the full
    dotted version string (``"150.0.7871.186"``).

    Pure and deterministic — no I/O — so it can be exercised without a
    live browser (property test 5.2). Raises :class:`LunaSpoofConfigError`
    naming the offending value when the field is empty or malformed
    (Req 14.9), so the launcher never falls back to a hardcoded version.
    """
    raw = (browser_field or "").strip()
    if not raw:
        raise LunaSpoofConfigError(
            "",
            "cannot determine installed browser version: /json/version "
            "reported no 'Browser' field",
        )
    match = _BROWSER_FIELD_RE.match(raw)
    if not match:
        raise LunaSpoofConfigError(
            raw,
            "cannot determine installed browser version: unrecognised "
            f"'Browser' value {raw!r}",
        )
    full_version = raw[match.start("major"):]
    return int(match.group("major")), full_version


def build_spoof_identity(config: LunaConfig, browser_field: str | None) -> SpoofIdentity:
    """Build the :class:`SpoofIdentity` from the config template and real version.

    Substitutes the real installed-browser major version into
    ``config.spoof_ua_template`` (its ``{version}`` placeholder) and
    derives the UA Client Hints version fields from the same real version
    (Req 14.8). Pure and deterministic given its inputs.

    Raises :class:`LunaSpoofConfigError` when the version cannot be parsed
    (via :func:`parse_browser_version`) or when the template carries no
    ``{version}`` placeholder — a template with a baked-in version would
    emit a hardcoded version, which Req 12.6/14.8 forbid.
    """
    major, full_version = parse_browser_version(browser_field)
    template = config.spoof_ua_template
    if _VERSION_PLACEHOLDER not in template:
        raise LunaSpoofConfigError(
            full_version,
            "spoof_ua_template lacks the "
            f"{_VERSION_PLACEHOLDER!r} placeholder; refusing to emit a "
            "hardcoded browser version",
        )
    user_agent = template.replace(_VERSION_PLACEHOLDER, str(major))
    return SpoofIdentity(
        major_version=major,
        full_version=full_version,
        user_agent=user_agent,
        ua_ch_major_version=str(major),
        ua_ch_full_version=full_version,
    )


def derive_browser_version(cdp: EdgeCDPClient) -> str:
    """Fetch the real installed-browser version string over CDP.

    Reuses :meth:`EdgeCDPClient.get_browser_version` (the ``/json/version``
    "Browser" field) rather than hand-rolling an HTTP call. This is the
    only part of version tracking that talks to a live browser; the
    returned string is handed to the pure :func:`build_spoof_identity`.

    Raises :class:`LunaSpoofConfigError` when the browser does not report a
    version so the launcher can abort (Req 14.9).
    """
    browser_field = cdp.get_browser_version()
    if not browser_field:
        raise LunaSpoofConfigError(
            "",
            "cannot determine installed browser version: CDP "
            "/json/version did not report a 'Browser' field",
        )
    return browser_field


# ── Pure override-payload builders (no I/O; unit/property testable) ──
#
# These construct the exact CDP parameters applied to every attached
# target. They take only the resolved config + runtime identity so they
# are deterministic and can be exercised without a browser (property 1's
# "full override" is these two payloads).

#: WebGL ``getParameter`` enum for the unmasked renderer string. The
#: ``WEBGL_debug_renderer_info`` extension exposes the real GPU name under
#: this numeric constant; Luna reads it to sniff a Linux GPU, so the
#: injected getter must answer the configured Windows renderer for it
#: (Req 14.6, 14.7).
_UNMASKED_RENDERER_WEBGL = 37446

#: The platform version reported in the UA Client Hints metadata. Luna
#: only gates on ``platform == "Windows"`` (Req 14.4); a plausible Windows
#: platform version keeps the metadata internally consistent.
_WINDOWS_PLATFORM_VERSION = "10.0.0"


def build_user_agent_metadata(
    config: LunaConfig, identity: SpoofIdentity,
) -> dict[str, Any]:
    """Build the ``userAgentMetadata`` for ``Emulation.setUserAgentOverride``.

    The metadata carries ``platform`` (the configured Windows value), the
    brand/version list derived from the real installed-browser major
    version (so UA Client Hints agree with the UA string — Req 14.3, 14.8),
    and a Windows ``platformVersion``. Pure given its inputs.

    This is the layer that defeats
    ``navigator.userAgentData.getHighEntropyValues(['platform'])`` (F3) —
    the UA string alone is not enough.
    """
    major = identity.ua_ch_major_version
    full = identity.ua_ch_full_version
    brands = [
        {"brand": "Chromium", "version": major},
        {"brand": "Google Chrome", "version": major},
        {"brand": "Not=A?Brand", "version": "99"},
    ]
    full_version_list = [
        {"brand": "Chromium", "version": full},
        {"brand": "Google Chrome", "version": full},
        {"brand": "Not=A?Brand", "version": "99.0.0.0"},
    ]
    return {
        "platform": config.spoof_uach_platform,
        "platformVersion": _WINDOWS_PLATFORM_VERSION,
        "architecture": "x86",
        "bitness": "64",
        "model": "",
        "mobile": False,
        "brands": brands,
        "fullVersionList": full_version_list,
        "uaFullVersion": full,
    }


def build_set_user_agent_override_params(
    config: LunaConfig, identity: SpoofIdentity,
) -> dict[str, Any]:
    """Build the ``Emulation.setUserAgentOverride`` params for a target.

    Carries both the spoofed UA string (version-tracked, Req 14.3/14.8) and
    the full ``userAgentMetadata`` (platform=Windows, brand/version list,
    platformVersion — Req 14.4). Pure given its inputs.
    """
    return {
        "userAgent": identity.user_agent,
        "userAgentMetadata": build_user_agent_metadata(config, identity),
    }


def build_platform_override_script(config: LunaConfig) -> str:
    """Build the JS injected before any page script via ``addScriptToEvaluateOnNewDocument``.

    Overrides ``navigator.platform`` to the configured Windows string
    (Req 14.5) and the WebGL ``UNMASKED_RENDERER_WEBGL`` (37446) getter to
    the configured Windows renderer (Req 14.6, 14.7), so a page cannot read
    back a Linux platform or GPU. Runs before any page script because it is
    installed via ``Page.addScriptToEvaluateOnNewDocument`` (and evaluated
    once for the already-loaded document).

    Pure given the config; the returned source embeds the config values via
    ``json.dumps`` so arbitrary renderer/platform strings are safely quoted.
    """
    navigator_platform = json.dumps(config.spoof_navigator_platform)
    webgl_renderer = json.dumps(config.spoof_webgl_renderer)
    return f"""
(() => {{
  try {{
    Object.defineProperty(navigator, 'platform', {{
      get: () => {navigator_platform},
      configurable: true,
    }});
  }} catch (e) {{}}
  const UNMASKED_RENDERER_WEBGL = {_UNMASKED_RENDERER_WEBGL};
  const spoofRenderer = {webgl_renderer};
  const patch = (proto) => {{
    if (!proto || !proto.getParameter) return;
    const original = proto.getParameter;
    proto.getParameter = function (parameter) {{
      if (parameter === UNMASKED_RENDERER_WEBGL) {{
        return spoofRenderer;
      }}
      return original.apply(this, arguments);
    }};
  }};
  try {{ patch(window.WebGLRenderingContext && window.WebGLRenderingContext.prototype); }} catch (e) {{}}
  try {{ patch(window.WebGL2RenderingContext && window.WebGL2RenderingContext.prototype); }} catch (e) {{}}
}})();
""".strip()


#: The JS expression evaluated by :meth:`LunaPlatformSpoofer.verify` after
#: the detail page loads and before the play-click (Req 14.11). It is the
#: exact signal Luna reads to gate Linux (F3): the high-entropy
#: ``platform`` from UA Client Hints, not the UA string. ``getHighEntropyValues``
#: returns a Promise, so the ``Runtime.evaluate`` call must set
#: ``awaitPromise`` and ``returnByValue``.
_VERIFY_PLATFORM_EXPRESSION = (
    "navigator.userAgentData"
    ".getHighEntropyValues(['platform'])"
    ".then(v => v.platform)"
)


def extract_platform_from_evaluate(result: dict[str, Any]) -> str:
    """Extract the reported platform from a ``Runtime.evaluate`` result.

    ``result`` is the payload returned by evaluating
    :data:`_VERIFY_PLATFORM_EXPRESSION` with ``awaitPromise`` and
    ``returnByValue``; its shape is
    ``{"result": {"type": "string", "value": "Windows"}}``. A thrown
    promise / evaluation error carries ``exceptionDetails`` instead.

    Pure and deterministic — no I/O — so the nested-shape handling is
    unit-testable without a browser. Returns the platform string (e.g.
    ``"Windows"`` on a spoofed session, ``"Linux"`` when the override did
    not take). Raises :class:`LunaSpoofConfigError` when the evaluation
    threw, reported no value, or returned a non-string, so the launch flow
    treats an unreadable probe as a spoofing failure rather than silently
    passing.
    """
    exception = result.get("exceptionDetails")
    if exception:
        raise LunaSpoofConfigError(
            "",
            "platform verification failed: getHighEntropyValues raised "
            f"{exception!r}",
        )
    inner = result.get("result")
    if not isinstance(inner, dict) or "value" not in inner:
        raise LunaSpoofConfigError(
            "",
            "platform verification failed: getHighEntropyValues returned "
            f"no value (result {result!r})",
        )
    value = inner.get("value")
    if not isinstance(value, str):
        raise LunaSpoofConfigError(
            "" if value is None else str(value),
            "platform verification failed: getHighEntropyValues returned a "
            f"non-string platform {value!r}",
        )
    return value


# ── Browser-level CDP session abstraction (the one I/O seam) ─────────
#
# ``attach()`` drives a BrowserCdpSession, not aiohttp directly, so the
# whole auto-attach + per-target override loop can be exercised against a
# mock session that emits a scripted set of targets and records per-target
# ordering (property 1's "override before resume"). The concrete aiohttp
# implementation is the only part that touches a live browser.


class BrowserCdpSession(Protocol):
    """A browser-level CDP session: send commands, consume attach events.

    Commands are routed to a target by ``session_id`` (CDP "flatten" mode);
    ``session_id=None`` addresses the browser session itself (used for the
    browser-level ``Target.setAutoAttach``).
    """

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Send one CDP command, return its result payload."""
        ...

    def attached_targets(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Yield ``(session_id, target_info)`` for each attached target."""
        ...


class LunaPlatformSpoofer:
    """Browser-level CDP OS-identity override for a whole Luna session.

    Constructed from :class:`LunaConfig` alone (no :class:`LunaStore`
    dependency) so it runs in the launcher subprocess. :meth:`attach`
    installs a browser-level ``Target.setAutoAttach`` with ``flatten`` and
    ``waitForDebuggerOnStart`` so **every** target — current, future, page
    or service worker — is caught, and applies the full platform override
    to each target **before** releasing it from waiting-for-debugger. A
    page-level-only override is lost on navigation to the game page (F3);
    browser-level auto-attach re-applies to the fresh target, which is why
    this must be browser-level.

    :meth:`verify` evaluates
    ``navigator.userAgentData.getHighEntropyValues(['platform'])`` on the
    active page after the detail page has loaded and **before** the
    play-click, and returns the reported platform string (Req 14.11). It
    does **not** decide the mismatch itself: the launch flow (task 7.4)
    compares the returned value to the configured
    ``spoof_uach_platform`` and maps a mismatch to
    ``platform_spoofing_failure`` (abort + log the reported value —
    Req 14.12, 11.4).
    """

    def __init__(self, config: LunaConfig) -> None:
        """Build the spoofer bound to a resolved :class:`LunaConfig`."""
        self.config = config
        self._identity: SpoofIdentity | None = None

    @property
    def identity(self) -> SpoofIdentity | None:
        """The runtime-derived identity, or ``None`` before :meth:`attach`."""
        return self._identity

    async def attach(self, cdp: EdgeCDPClient) -> None:
        """Install the browser-level override for the whole session (Req 14.1).

        Derives the real installed-browser version over ``cdp`` (Req 14.8,
        aborting via :class:`LunaSpoofConfigError` if undeterminable),
        resolves the browser websocket on ``browser_game_cdp_port()``, opens
        a :class:`BrowserCdpSession` over it and drives the auto-attach loop.

        Raises :class:`LunaSpoofConfigError` when the browser version cannot
        be determined (the launcher maps this to
        ``platform_spoofing_failure`` and aborts — Req 14.9, 14.13).
        """
        identity = build_spoof_identity(self.config, derive_browser_version(cdp))
        self._identity = identity
        ws_url = cdp.get_browser_ws_url()
        if not ws_url:
            raise LunaSpoofConfigError(
                "",
                "cannot attach platform spoofer: no browser websocket URL "
                "on the browser-game CDP port",
            )
        session = _AiohttpBrowserCdpSession(ws_url)
        try:
            await self.run_attach(session, identity)
        finally:
            await session.close()

    async def run_attach(
        self, session: BrowserCdpSession, identity: SpoofIdentity,
    ) -> None:
        """Drive the auto-attach loop over an arbitrary ``session`` (Req 14.1-14.7).

        Separated from :meth:`attach` so it can be driven against a mock
        :class:`BrowserCdpSession` (property 1 / task 5.4): the loop sends
        the browser-level ``Target.setAutoAttach`` and, for every attached
        target, applies the full override before resuming it. Carrying
        ``identity`` explicitly keeps this method free of any live-browser
        dependency.
        """
        await session.send(
            "Target.setAutoAttach",
            {
                "autoAttach": True,
                "flatten": True,
                "waitForDebuggerOnStart": True,
            },
        )
        async for session_id, target_info in session.attached_targets():
            await self._apply_override_to_target(
                session, session_id, target_info, identity,
            )

    async def verify(self, cdp: EdgeCDPClient) -> str:
        """Report the platform Luna sees on the loaded detail page (Req 14.11).

        Evaluates
        ``navigator.userAgentData.getHighEntropyValues(['platform'])`` on
        the active Luna page target — the signal Luna gates Linux on (F3) —
        and returns the reported platform string (e.g. ``"Windows"``). Must
        be called *after* the detail page has loaded and *before* the
        play-click.

        This method does **not** decide the mismatch: the launch flow
        (task 7.4) compares the returned value to
        ``config.spoof_uach_platform`` and maps a mismatch to
        ``platform_spoofing_failure`` (abort + log the reported value —
        Req 14.12, 11.4). It mirrors :meth:`attach`: resolve the browser
        websocket on ``browser_game_cdp_port()``, open a
        :class:`BrowserCdpSession`, and attach to the page target to run the
        evaluate against it.

        Raises :class:`LunaSpoofConfigError` when no page target or browser
        websocket is available, or when the probe cannot be read, so the
        launcher aborts rather than proceeding on an unverified session.
        """
        ws_url = cdp.get_browser_ws_url()
        if not ws_url:
            raise LunaSpoofConfigError(
                "",
                "cannot verify platform: no browser websocket URL on the "
                "browser-game CDP port",
            )
        session = _AiohttpBrowserCdpSession(ws_url)
        try:
            return await self.run_verify(session)
        finally:
            await session.close()

    async def run_verify(self, session: BrowserCdpSession) -> str:
        """Evaluate the platform probe over an arbitrary ``session`` (Req 14.11).

        Separated from :meth:`verify` so it can be driven against a mock
        :class:`BrowserCdpSession`: it attaches to the active page target,
        evaluates :data:`_VERIFY_PLATFORM_EXPRESSION` with ``awaitPromise``
        and ``returnByValue`` (``getHighEntropyValues`` is async), and hands
        the raw CDP result to the pure
        :func:`extract_platform_from_evaluate` for the value extraction.
        """
        page_session_id = await self._attach_to_page_target(session)
        result = await session.send(
            "Runtime.evaluate",
            {
                "expression": _VERIFY_PLATFORM_EXPRESSION,
                "awaitPromise": True,
                "returnByValue": True,
            },
            session_id=page_session_id,
        )
        platform = extract_platform_from_evaluate(result)
        logger.info("[Luna] platform verification reported %r", platform)
        return platform

    async def _attach_to_page_target(self, session: BrowserCdpSession) -> str:
        """Return a CDP session id attached to the active Luna page target.

        Asks the browser for its targets and attaches (flatten mode) to the
        first ``page`` target — the loaded Luna detail page the probe must
        run against. Raises :class:`LunaSpoofConfigError` when no page
        target exists so :meth:`verify` aborts the launch (Req 14.13).

        Delegates the shared CDP page-target-attach step to
        :func:`unifideck.stores.luna.luna_cdp.attach_to_page_target`, passing
        the spoofer's own :class:`LunaSpoofConfigError` for each failure.
        """
        return await attach_to_page_target(
            session,
            raise_no_target=lambda: LunaSpoofConfigError(
                "",
                "cannot verify platform: no page target found on the "
                "browser-game CDP port",
            ),
            raise_no_session=lambda: LunaSpoofConfigError(
                "",
                "cannot verify platform: failed to attach to the Luna page "
                "target",
            ),
        )

    async def _apply_override_to_target(
        self,
        session: BrowserCdpSession,
        session_id: str,
        target_info: dict[str, Any],
        identity: SpoofIdentity,
    ) -> None:
        """Apply the full override to one target, then resume it (Req 14.2-14.7).

        Ordering is the invariant property 1 asserts: the UA/UA-CH override
        **and** the pre-document script are applied *before*
        ``Runtime.runIfWaitingForDebugger`` releases the target. The
        pre-document script is additionally evaluated once for the current
        document so an already-loaded page is covered too.

        The ``Emulation.setUserAgentOverride`` is the core of the spoof and is
        broadly supported, so it is always applied to every target. The
        ``Page.*`` + current-document steps, however, are only meaningful on
        targets that expose the CDP ``Page`` domain: a ``background_page``,
        ``service_worker``, ``worker`` or ``other`` target can reject
        ``Page.enable`` with ``-32601`` ("'Page.enable' wasn't found"). That
        domain-not-found rejection must not abort the whole attach (it would
        fail the entire sign-in — the real on-device failure), so it is
        tolerated per-target: the UA override still stands and the target is
        still resumed. Any *other* error (or a failure of the always-applied
        steps) still propagates.
        """
        ua_params = build_set_user_agent_override_params(self.config, identity)
        target_type = str(target_info.get("type", "?"))

        await session.send(
            "Emulation.setUserAgentOverride", ua_params, session_id=session_id,
        )
        try:
            await self._apply_page_override(session, session_id)
            logger.info(
                "[Luna] platform override applied to %s target (session %s)",
                target_type, session_id,
            )
        except RuntimeError as exc:
            if not _is_domain_not_found(exc):
                raise
            logger.info(
                "[Luna] Page domain unavailable on %s target "
                "(session %s) — applied UA override only",
                target_type, session_id,
            )
        # Always resume: a target left paused in waiting-for-debugger (a
        # background_page in particular) can hang the whole browser.
        await session.send(
            "Runtime.runIfWaitingForDebugger", {}, session_id=session_id,
        )

    async def _apply_page_override(
        self, session: BrowserCdpSession, session_id: str,
    ) -> None:
        """Apply the ``Page.*`` + current-document override to one target.

        Enables the ``Page`` domain, installs the override script so it runs
        before any script of every future document
        (``addScriptToEvaluateOnNewDocument``), and evaluates it once for the
        already-loaded document. Raises :class:`RuntimeError` on any CDP
        error, including the ``-32601`` domain-not-found the caller tolerates
        for targets without the ``Page`` domain.
        """
        script_source = build_platform_override_script(self.config)
        await session.send("Page.enable", {}, session_id=session_id)
        await session.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": script_source},
            session_id=session_id,
        )
        await session.send(
            "Runtime.evaluate",
            {"expression": script_source},
            session_id=session_id,
        )


class _AiohttpBrowserCdpSession:
    """aiohttp-backed :class:`BrowserCdpSession` over the browser websocket.

    Mirrors the established CDP-over-websocket pattern in this codebase
    (``launcher/cdp/cdp_primitives.py`` / ``cdp/page_inject.py``): one
    long-lived ``aiohttp`` websocket to the browser endpoint, monotonically
    increasing message ids, ``sessionId`` routing for flatten mode.
    Command replies and ``Target.attachedToTarget`` events share the one
    socket, so :meth:`send` skips over events it is not waiting for and
    :meth:`attached_targets` buffers replies it is not the recipient of.
    """

    def __init__(self, ws_url: str) -> None:
        """Bind to a browser-level CDP websocket URL (not yet connected)."""
        self._ws_url = ws_url
        self._session: Any = None
        self._ws: Any = None
        self._next_id = 0
        self._pending_events: list[tuple[str, dict[str, Any]]] = []

    async def _ensure_ws(self) -> Any:
        """Open the websocket on first use (lazy connect)."""
        if self._ws is not None:
            return self._ws
        import aiohttp
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(
            self._ws_url, heartbeat=10, autoping=True,
        )
        return self._ws

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Send one CDP command and return its ``result`` payload.

        Routes to a target via ``sessionId`` when given (flatten mode).
        Interleaved ``Target.attachedToTarget`` events are buffered so a
        concurrent :meth:`attached_targets` consumer does not lose them.
        """
        import aiohttp
        ws = await self._ensure_ws()
        self._next_id += 1
        msg_id = self._next_id
        message: dict[str, Any] = {
            "id": msg_id, "method": method, "params": params or {},
        }
        if session_id is not None:
            message["sessionId"] = session_id
        await ws.send_json(message)
        while True:
            raw = await ws.receive(timeout=15)
            if raw.type != aiohttp.WSMsgType.TEXT:
                if raw.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    raise RuntimeError("CDP browser websocket closed")
                continue
            payload = json.loads(raw.data)
            if payload.get("method") == "Target.attachedToTarget":
                self._buffer_event(payload)
                continue
            if payload.get("id") != msg_id:
                continue
            if "error" in payload:
                raise RuntimeError(f"{method} failed: {payload['error']}")
            result = payload.get("result")
            return result if isinstance(result, dict) else {}

    def _buffer_event(self, payload: dict[str, Any]) -> None:
        """Record a ``Target.attachedToTarget`` event for later consumption."""
        params = payload.get("params") or {}
        session_id = params.get("sessionId")
        target_info = params.get("targetInfo") or {}
        if session_id:
            self._pending_events.append((session_id, target_info))

    async def attached_targets(
        self,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Yield ``(session_id, target_info)`` as targets attach.

        Drains any events buffered by :meth:`send` first, then reads the
        browser websocket for further ``Target.attachedToTarget`` events.

        Bounded by an **idle timeout**: ``Target.setAutoAttach`` delivers one
        ``attachedToTarget`` burst for the already-open targets, then the
        browser ws goes silent. An unbounded ``ws.receive()`` therefore blocks
        forever and ``run_attach`` never returns (the on-device scrape hang).
        Ending the iteration once no further target has attached for
        :data:`_ATTACH_IDLE_TIMEOUT_S` lets the spoofer finish the setup loop
        and the scrape proceed, while still covering every target the burst
        delivered.
        """
        import asyncio

        import aiohttp
        while self._pending_events:
            yield self._pending_events.pop(0)
        ws = await self._ensure_ws()
        while True:
            try:
                raw = await ws.receive(timeout=_ATTACH_IDLE_TIMEOUT_S)
            except (TimeoutError, asyncio.TimeoutError):
                # No further target attached within the idle window — the
                # auto-attach burst is done. End setup rather than block.
                return
            if raw.type != aiohttp.WSMsgType.TEXT:
                if raw.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    return
                continue
            payload = json.loads(raw.data)
            if payload.get("method") != "Target.attachedToTarget":
                continue
            params = payload.get("params") or {}
            session_id = params.get("sessionId")
            target_info = params.get("targetInfo") or {}
            if session_id:
                yield session_id, target_info

    async def close(self) -> None:
        """Close the websocket and its aiohttp session."""
        import contextlib
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
