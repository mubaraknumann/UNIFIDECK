"""Steam-frontend CSS injection via CDP.

OP-13a | py_modules/unifideck/cdp/cdp_inject.py

Locates Steam's frontend tab (identified by the
``steamloopback.host`` URL marker), connects to it via
CDP, and injects/removes CSS rules that hide native Play
buttons on games owned through Unifideck's external
stores.

The injected CSS is identified by a per-marker ``<style>``
element id (``unifideck-style-<marker>``) so the injection
is idempotent — repeated calls update the existing
element rather than stacking duplicates.

``HIDE_PLAY_CSS`` is the actual ruleset, with
``__APP_ID__`` placeholders that get replaced per call.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .cdp_client import CDPClient

logger = logging.getLogger(__name__)

STEAM_TAB_URL_MARKER = "steamloopback.host"
STYLE_ID_PREFIX = "unifideck-style-"
HIDE_PLAY_CSS = """
div[class*="appactionbutton_PlayButton"][data-app-id="__APP_ID__"],
div[class*="library_AppActionButton"][data-app-id="__APP_ID__"]
button[class*="play_PlayBtn"] {
    display: none !important;
}
""".strip()


def is_steam_ui_tab(page: dict[str, Any]) -> bool:
    """Return whether a CDP target descriptor refers to Steam's UI tab.

    Identity check: the page's URL contains the
    ``steamloopback.host`` marker. Type-defensive
    (returns False for non-dict input).

    Args:
        page: CDP target descriptor (one entry from
            ``/json``).

    Returns:
        True if it's Steam's frontend tab.
    """
    if not isinstance(page, dict):
        return False
    url = page.get("url", "")
    return STEAM_TAB_URL_MARKER in url


def escape_css_for_template_literal(css: str) -> str:
    """Escape ``css`` so it can be embedded in a JS template literal.

    Three escapes:

    * ``\\`` → ``\\\\``;
    * `` ` `` → ``\\ ` ``;
    * ``${`` → ``\\${``.

    The last one prevents template-literal interpolation
    inside the CSS (rare but possible if a class name
    contains ``${``).

    Args:
        css: raw CSS string.

    Returns:
        JS-safe escaped string.
    """
    return css.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")


def build_marker_id(name: str) -> str:
    """Build a DOM-safe ``id`` from a marker name.

    Strips characters that aren't alphanumeric, dash,
    or underscore (replacing them with ``_``) and
    prepends the ``unifideck-style-`` namespace. The
    result is a valid HTML id.

    Args:
        name: caller-supplied marker name.

    Returns:
        Namespaced safe id string.
    """
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return f"{STYLE_ID_PREFIX}{safe}"


class SteamCSSInjector:
    """CDP-backed CSS injector targeting Steam's frontend tab."""

    def __init__(self, cdp_client: CDPClient) -> None:
        """Bind an existing CDP client.

        The client is injected (not constructed) so the
        same client can be shared with other CDP-based
        features.

        Args:
            cdp_client: a ``CDPClient`` instance.
        """
        self._cdp = cdp_client

    async def connect_to_steam(self) -> bool:
        """Open a CDP session against Steam's UI tab.

        Catches any exception so the caller doesn't
        need to wrap. Failure logs at WARN — connect
        errors during boot are expected (Steam not
        ready yet) and shouldn't break boot.

        Returns:
            True on success.
        """
        try:
            return await self._cdp.connect(STEAM_TAB_URL_MARKER)
        except Exception as e:
            logger.warning("[cdp_inject] connect failed: %s", e)
            return False

    async def inject_css(self, css: str, marker: str) -> bool:
        """Insert/update a ``<style>`` element with ``css`` under ``marker``.

        Idempotent: looks up the id, creates the
        ``<style>`` element if absent, updates its
        ``textContent`` in any case. The JS payload is
        an IIFE returning ``true`` so the CDP
        ``Runtime.evaluate`` response carries a clean
        success signal.

        Args:
            css: CSS source.
            marker: per-injection identifier (used to
                build the DOM id).

        Returns:
            True on successful eval, False on CDP
            error.
        """
        marker_id = build_marker_id(marker)
        escaped = escape_css_for_template_literal(css)
        js = f"""
                                (() => {{
                                const id = "{marker_id}";
                                let el = document.getElementById(id);
                                if (!el) {{
                                el = document.createElement("style");
                                el.id = id;
                                document.head.appendChild(el);
                                }}
                                el.textContent = `{escaped}`;
                                return true;
                                }})()
                                """
        try:
            result = await self._cdp.eval_js(js)
            return bool(result)
        except Exception as e:
            logger.warning(
                "[cdp_inject] eval failed for %s: %s",
                marker,
                e,
            )
            return False

    async def remove_css(self, marker: str) -> bool:
        """Remove the ``<style>`` element for ``marker`` if present.

        Returns False when the element didn't exist
        (idempotent no-op). DEBUG log on CDP error
        (less noise than WARN — removal failures are
        less actionable).

        Args:
            marker: identifier used at injection time.

        Returns:
            True if the element was found and removed.
        """
        marker_id = build_marker_id(marker)
        js = f"""
                                (() => {{
                                const el = document.getElementById("{marker_id}");
                                if (el) {{ el.remove(); return true; }}
                                return false;
                                }})()
                                """
        try:
            return bool(await self._cdp.eval_js(js))
        except Exception as e:
            logger.debug(
                "[cdp_inject] remove failed for %s: %s",
                marker,
                e,
            )
            return False

    async def hide_play_section(self, app_id: int) -> bool:
        """Inject the play-section-hiding CSS for ``app_id``.

        Builds the rule by substituting ``app_id``
        into the ``HIDE_PLAY_CSS`` template, then
        injects under the per-app marker
        ``app-<app_id>``.

        Args:
            app_id: Steam AppID.

        Returns:
            True on successful injection.
        """
        css = HIDE_PLAY_CSS.replace("__APP_ID__", str(app_id))
        return await self.inject_css(css, f"app-{app_id}")

    async def show_play_section(self, app_id: int) -> bool:
        """Remove the play-section-hiding CSS for ``app_id``.

        Args:
            app_id: Steam AppID.

        Returns:
            True if the rule was present and removed.
        """
        return await self.remove_css(f"app-{app_id}")


_singleton_injector: SteamCSSInjector | None = None


async def get_cdp_client() -> SteamCSSInjector:
    """Return the module-level injector singleton, building it lazily.

    Constructs both the underlying ``CDPClient`` and
    the wrapping ``SteamCSSInjector`` on first call.
    The import is deferred to avoid loading the
    websockets-heavy CDP client at module import time.

    Returns:
        Cached ``SteamCSSInjector``.
    """
    global _singleton_injector
    if _singleton_injector is None:
        from .cdp_client import CDPClient

        client = CDPClient()
        _singleton_injector = SteamCSSInjector(client)
    return _singleton_injector


async def shutdown_cdp_client() -> None:
    """Drop the singleton so the next ``get_cdp_client`` rebuilds it.

    Doesn't explicitly disconnect — relies on garbage
    collection to close the websocket. Callers needing
    a clean disconnect should call
    ``client.disconnect()`` themselves before this.
    """
    global _singleton_injector
    _singleton_injector = None
