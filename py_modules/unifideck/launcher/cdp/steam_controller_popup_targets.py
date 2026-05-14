"""CDP target acquisition for the Steam controller popup workflow."""

from __future__ import annotations
import asyncio
import logging
import aiohttp
from .cdp_primitives import (
    cdp_command,
    evaluate_in_target,
    wait_for_titled_target,
)
logger = logging.getLogger(__name__)
_STEAM_SHARED_CONTEXT_TITLE = "SharedJSContext"
async def open_controller_popup(steam_port: int, appid: int) -> None:
    """Open Steam's Controller Configurator popup for one AppID.

    Evaluates a JS snippet inside Steam's SharedJSContext target
    that calls ``SteamClient.Apps.ShowControllerConfigurator``.

    Args:
        steam_port: CDP port of the Steam client.
        appid: AppID to open the configurator for.

    Raises:
        RuntimeError: SharedJSContext target not found.
    """
    shared_target = await wait_for_titled_target(
        steam_port,
        _STEAM_SHARED_CONTEXT_TITLE,
        timeout=10.0,
    )
    if not shared_target:
        raise RuntimeError("SharedJSContext target not found")
    expression = (
        f"(async () => {{ "
        f"window.SteamClient?.Apps?.ShowControllerConfigurator?.({appid}); "
        f"return 'opened'; "
        f"}})()"
    )
    await evaluate_in_target(shared_target, expression)
async def wait_for_popup_root_ready(
    websocket: aiohttp.ClientWebSocketResponse,
) -> bool:
    """Poll the popup body until the ``View Layout`` button is rendered.

    Indicates that the popup's root page (not a sub-screen) is
    ready for the bounce sequence. ~15s total (60×0.25s).

    Args:
        websocket: Popup CDP websocket.

    Returns:
        True iff the root page rendered within the deadline.
    """
    for attempt in range(60):
        try:
            resp = await cdp_command(
                websocket,
                3000 + attempt,
                "Runtime.evaluate",
                {
                    "expression": (
                        "Array.from("
                        "document.querySelectorAll('button,[role=\"link\"]')"
                        ").some((e) => (e.textContent || '').trim() === "
                        "'View Layout')"
                    ),
                    "returnByValue": True,
                },
            )
            value = (
                resp.get("result", {}).get("result", {}).get("value")
            )
            if value is True:
                return True
        except Exception as exc:
            logger.debug("[popup] root poll failed: %s", exc)
        await asyncio.sleep(0.25)
    return False