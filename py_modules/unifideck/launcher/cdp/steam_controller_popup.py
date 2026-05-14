"""Steam controller popup orchestration — opens, configures, and refreshes the controller layout popup via CDP."""

from __future__ import annotations
import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any
import aiohttp
from .cdp_primitives import (
    close_target,
    close_titled_targets,
    wait_for_titled_target,
)
from .steam_controller_popup_fiber import (
    inspect_popup_state,
    preview_popup_config,
    resolve_popup_preview_context,
    set_active_popup_config,
)
from .steam_controller_popup_targets import (
    open_controller_popup,
    wait_for_popup_root_ready,
)
logger = logging.getLogger(__name__)
_STEAM_CONTROLLER_LAYOUT_TITLE = "Controller Layout"
_WASD_TEMPLATE_URL = "template://controller_neptune_wasd.vdf"
_JOYSTICK_TEMPLATE_URL = "template://controller_neptune_gamepad_fps.vdf"
_PostBounceHook = Callable[[], Awaitable[Any]] | Callable[[], Any] | None
async def _phase1_preview_wasd(
    websocket: aiohttp.ClientWebSocketResponse,
    h_v3_object: str,
    shortcut_appid: int,
    controller_index: int,
    dwell: float,
) -> dict[str, Any]:
    """Preview the WASD controller template, then sample the popup state.

    Args:
        websocket: Popup CDP websocket.
        h_v3_object: Steam's ``ControllerConfigurator.Summary``
            scope object handle.
        shortcut_appid: AppID of the non-Steam shortcut.
        controller_index: Controller slot index.
        dwell: Seconds to wait after preview before inspection.

    Returns:
        Popup state dict (title/url/usesMouse/...).
    """
    await preview_popup_config(
        websocket,
        h_v3_object,
        shortcut_appid,
        controller_index,
        _WASD_TEMPLATE_URL,
        msg_id=2001,
    )
    await asyncio.sleep(dwell)
    return await inspect_popup_state(websocket, msg_id=2002)
async def _phase2_activate_joystick(
    websocket: aiohttp.ClientWebSocketResponse,
    h_v3_object: str,
    shortcut_appid: int,
    controller_index: int,
) -> None:
    """Make the joystick template the saved/active configuration.

    Args:
        websocket: Popup CDP websocket.
        h_v3_object: Steam's configurator scope handle.
        shortcut_appid: AppID of the non-Steam shortcut.
        controller_index: Controller slot index.
    """
    await set_active_popup_config(
        websocket,
        h_v3_object,
        shortcut_appid,
        controller_index,
        _JOYSTICK_TEMPLATE_URL,
        msg_id=2003,
    )
    await asyncio.sleep(0.5)

async def _phase3_confirm_joystick(
    websocket: aiohttp.ClientWebSocketResponse,
    h_v3_object: str,
    shortcut_appid: int,
    controller_index: int,
) -> dict[str, Any]:

    """Preview the joystick template and sample the resulting state.

    Args:
        websocket: Popup CDP websocket.
        h_v3_object: Steam's configurator scope handle.
        shortcut_appid: AppID of the non-Steam shortcut.
        controller_index: Controller slot index.

    Returns:
        Popup state dict to verify the URL actually changed.
    """
    await preview_popup_config(
        websocket,
        h_v3_object,
        shortcut_appid,
        controller_index,
        _JOYSTICK_TEMPLATE_URL,
        msg_id=2004,
    )
    await asyncio.sleep(0.75)
    return await inspect_popup_state(websocket, msg_id=2005)
async def _run_bounce_sequence(
    websocket: aiohttp.ClientWebSocketResponse,
    shortcut_appid: int,
    dwell: float,
) -> bool:
    """Run the WASD→joystick bounce sequence inside the popup.

    Steps: wait for the popup body to be ready → resolve the
    configurator scope and controller index → preview WASD →
    set active joystick → confirm joystick.

    Args:
        websocket: Popup CDP websocket.
        shortcut_appid: AppID of the non-Steam shortcut.
        dwell: Seconds to wait after the WASD preview.

    Returns:
        True iff the final state's URL matches the joystick
        template URL.

    Raises:
        RuntimeError: popup root never became ready.
    """
    if not await wait_for_popup_root_ready(websocket):
        raise RuntimeError(
            "Controller Layout popup never reached the root page",
        )
    h_v3_object, controller_index = await resolve_popup_preview_context(
        websocket,
    )
    logger.info(
        "[popup] using controller index %s for AppID %s",
        controller_index,
        shortcut_appid,
    )
    wasd_state = await _phase1_preview_wasd(
        websocket, h_v3_object, shortcut_appid, controller_index, dwell,
    )
    logger.info(
        "[popup] after-wasd title=%s url=%s",
        wasd_state.get("title"), wasd_state.get("url"),
    )
    await _phase2_activate_joystick(
        websocket, h_v3_object, shortcut_appid, controller_index,
    )
    final_state = await _phase3_confirm_joystick(
        websocket, h_v3_object, shortcut_appid, controller_index,
    )
    logger.info(
        "[popup] after-joystick title=%s url=%s",
        final_state.get("title"), final_state.get("url"),
    )
    return final_state.get("url") == _JOYSTICK_TEMPLATE_URL
async def _open_popup_and_run_bounce(
    steam_port: int,
    shortcut_appid: int,
    dwell: float,
) -> tuple[bool, str | None]:
    """Open the controller-layout popup and run the bounce sequence.

    Args:
        steam_port: CDP port of the Steam client.
        shortcut_appid: AppID of the non-Steam shortcut.
        dwell: Seconds to wait between the WASD preview and the
            joystick activation.

    Returns:
        Tuple ``(success, popup_target_id)``.

    Raises:
        RuntimeError: popup target never appeared.
    """
    logger.info(
        "[popup] opening controller configurator for AppID %s",
        shortcut_appid,
    )
    await open_controller_popup(steam_port, shortcut_appid)
    popup_target = await wait_for_titled_target(
        steam_port, _STEAM_CONTROLLER_LAYOUT_TITLE, timeout=15.0,
    )
    if not popup_target:
        raise RuntimeError("Controller Layout popup did not open")
    popup_target_id = str(popup_target["id"])
    async with aiohttp.ClientSession() as session, session.ws_connect(
        popup_target["webSocketDebuggerUrl"], heartbeat=10, autoping=True,
    ) as websocket:
        success = await _run_bounce_sequence(
            websocket, shortcut_appid, dwell,
        )
    return success, popup_target_id
async def _close_popup(steam_port: int, popup_target_id: str | None) -> None:
    """Close the controller-layout popup target after the bounce.

    Closes by ID if known, else by title-substring sweep.

    Args:
        steam_port: CDP port of the Steam client.
        popup_target_id: Target ID, or ``None`` to sweep by title.
    """
    if popup_target_id:
        await close_target(steam_port, popup_target_id)
    else:
        await close_titled_targets(
            steam_port, _STEAM_CONTROLLER_LAYOUT_TITLE,
        )

async def _invoke_post_bounce_hook(on_complete: _PostBounceHook) -> None:

    """Call the optional post-bounce callback, awaiting if it's a coroutine.

    Exceptions are logged and swallowed.

    Args:
        on_complete: Sync or async callable, or ``None``.
    """
    if on_complete is None:
        return
    try:
        result = on_complete()
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:
        logger.debug("[popup] on_complete hook failed: %s", exc)
async def refresh_steam_controller_layout(
    steam_port: int,
    shortcut_appid: int,
    *,
    delay: float,
    dwell: float,
    on_complete: _PostBounceHook = None,
) -> bool:
    """Force-refresh a non-Steam shortcut's controller layout.

    Workaround for Steam not picking up a fresh controller
    config until the layout popup is opened. Opens the popup,
    bounces the config WASD→joystick, then closes the popup
    again. Invokes an optional ``on_complete`` callback (e.g.
    to refocus the xCloud window).

    Args:
        steam_port: CDP port of the Steam client.
        shortcut_appid: AppID of the non-Steam shortcut
            (must be > 0).
        delay: Initial sleep before the bounce.
        dwell: Seconds to wait between phases.
        on_complete: Optional post-bounce hook.

    Returns:
        True iff the bounce verified the joystick config is
        now active.
    """
    if shortcut_appid <= 0:
        return False
    await asyncio.sleep(delay)
    await close_titled_targets(steam_port, _STEAM_CONTROLLER_LAYOUT_TITLE)
    popup_target_id: str | None = None
    success = False
    try:
        success, popup_target_id = await _open_popup_and_run_bounce(
            steam_port, shortcut_appid, dwell,
        )
    except Exception as exc:
        logger.exception("[popup] bounce failed: %s", exc)
    finally:
        await _close_popup(steam_port, popup_target_id)
        await _invoke_post_bounce_hook(on_complete)
    return success