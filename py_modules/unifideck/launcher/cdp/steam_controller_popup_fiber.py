"""Fiber-tree introspection helpers for the Steam controller popup — locate React handlers and trigger config previews."""

from __future__ import annotations
import contextlib
import logging
from typing import Any
import aiohttp
from .cdp_primitives import cdp_command
logger = logging.getLogger(__name__)
async def _click_handler_object_id(
    websocket: aiohttp.ClientWebSocketResponse,
    msg_id: int,
) -> str:
    """Locate the React onClick handler that opens the configurator preview.

    Walks the React fiber tree up from the ``View Layout`` button
    looking for an onClick whose source string mentions
    ``ControllerConfigurator.Summary``.

    Args:
        websocket: Popup CDP websocket.
        msg_id: CDP message ID for this call.

    Returns:
        CDP remote object ID of the onClick function.

    Raises:
        RuntimeError: Button or matching onClick not found.
    """
    resp = await cdp_command(
        websocket,
        msg_id,
        "Runtime.evaluate",
        {
            "expression": r"""(() => {
                const node = Array.from(
                    document.querySelectorAll('button,[role="link"]')
                ).find((element) => (element.textContent || '').trim() === 'View Layout');
                if (!node) {
                    return null;
                }
                const fiberKey = Object.keys(node).find((key) => key.startsWith('__reactFiber'));
                let fiber = fiberKey ? node[fiberKey] : null;
                while (fiber) {
                    const props = fiber.memoizedProps || {};
                    if (
                        typeof props.onClick === 'function' &&
                        String(props.onClick).includes('ControllerConfigurator.Summary')
                    ) {
                        return props.onClick;
                    }
                    fiber = fiber.return;
                }
                return null;
            })()""",
            "awaitPromise": True,
            "returnByValue": False,
            "userGesture": True,
        },
    )
    object_id = resp.get("result", {}).get("result", {}).get("objectId")
    if not object_id:
        raise RuntimeError("Could not resolve controller popup preview context")
    return str(object_id)

async def _scopes_object_id(
    websocket: aiohttp.ClientWebSocketResponse,
    on_click_object: str,
    msg_id: int,
) -> str:

    """Get the ``[[Scopes]]`` internal property handle of the onClick closure.

    Args:
        websocket: Popup CDP websocket.
        on_click_object: Object ID of the onClick function.
        msg_id: CDP message ID.

    Returns:
        Remote object ID of the scopes array.

    Raises:
        RuntimeError: ``[[Scopes]]`` not found on the function.
    """
    resp = await cdp_command(
        websocket,
        msg_id,
        "Runtime.getProperties",
        {
            "objectId": on_click_object,
            "ownProperties": False,
            "generatePreview": True,
        },
    )
    scopes_object = next(
        (
            item["value"]["objectId"]
            for item in resp.get("result", {}).get("internalProperties", [])
            if item.get("name") == "[[Scopes]]"
        ),
        None,
    )
    if not scopes_object:
        raise RuntimeError("Could not inspect controller popup scopes")
    return str(scopes_object)
async def _scope1_object_id(
    websocket: aiohttp.ClientWebSocketResponse,
    scopes_object: str,
    msg_id: int,
) -> str:
    """Return the object ID of the first scope in a ``[[Scopes]]`` array.

    Args:
        websocket: Popup CDP websocket.
        scopes_object: Object ID of the scopes array.
        msg_id: CDP message ID.

    Returns:
        Remote object ID of scope[1] (the closure scope
        containing the configurator object).
    """
    resp = await cdp_command(
        websocket,
        msg_id,
        "Runtime.getProperties",
        {
            "objectId": scopes_object,
            "ownProperties": True,
            "generatePreview": True,
        },
    )
    scope1_object = next(
        (
            item["value"]["objectId"]
            for item in resp.get("result", {}).get("result", [])
            if item.get("name") == "1"
        ),
        None,
    )
    if not scope1_object:
        raise RuntimeError("Could not resolve configurator module scope")
    return str(scope1_object)
async def _scope1_h_object_id(
    websocket: aiohttp.ClientWebSocketResponse,
    scope1_object: str,
    msg_id: int,
) -> str:
    """Return the object ID of the ``h`` (configurator) attribute in scope[1].

    Args:
        websocket: Popup CDP websocket.
        scope1_object: Object ID of scope[1].
        msg_id: CDP message ID.

    Returns:
        Remote object ID of the ``h`` value.

    Raises:
        RuntimeError: ``h`` not found in the scope.
    """
    resp = await cdp_command(
        websocket,
        msg_id,
        "Runtime.getProperties",
        {
            "objectId": scope1_object,
            "ownProperties": True,
            "generatePreview": True,
        },
    )
    scope_lookup = {
        prop["name"]: prop.get("value", {}).get("objectId")
        for prop in resp.get("result", {}).get("result", [])
        if prop.get("value", {}).get("objectId")
    }
    h_object = scope_lookup.get("h")
    if not h_object:
        raise RuntimeError("Could not resolve h.v3 controller helper")
    return str(h_object)

async def _h_v3_object_id(
    websocket: aiohttp.ClientWebSocketResponse,
    h_object: str,
    msg_id: int,
) -> tuple[str, int]:

    """Resolve the ``h.v3`` sub-object handle and the controller slot index.

    After fetching the ``v3`` property of ``h`` it also probes
    for the active controller index used by the configurator
    (defaults to 0 if probing fails).

    Args:
        websocket: Popup CDP websocket.
        h_object: Object ID of ``h``.
        msg_id: CDP message ID.

    Returns:
        Tuple ``(v3_object, controller_index)``.

    Raises:
        RuntimeError: ``v3`` not found on ``h``.
    """
    resp = await cdp_command(
        websocket,
        msg_id,
        "Runtime.getProperties",
        {
            "objectId": h_object,
            "ownProperties": True,
            "generatePreview": True,
        },
    )
    v3_object = next(
        (
            prop.get("value", {}).get("objectId")
            for prop in resp.get("result", {}).get("result", [])
            if prop.get("name") == "v3"
        ),
        None,
    )
    if not v3_object:
        raise RuntimeError("Could not resolve h.v3 sub-object")
    controller_index = 0
    with contextlib.suppress(Exception):
        v3_props = await cdp_command(
            websocket,
            msg_id + 1,
            "Runtime.getProperties",
            {"objectId": v3_object, "ownProperties": True},
        )
        for prop in v3_props.get("result", {}).get("result", []):
            if prop.get("name") == "currentController":
                value = prop.get("value", {}).get("value")
                if isinstance(value, int):
                    controller_index = value
                    break
    return str(v3_object), controller_index
async def resolve_popup_preview_context(
    websocket: aiohttp.ClientWebSocketResponse,
) -> tuple[str, int]:
    """Resolve the configurator scope and controller index handles.

    Walks: button → onClick → ``[[Scopes]]`` → scope[1] → reads
    the ``h`` (configurator) and ``v`` (controller index) values.

    Args:
        websocket: Popup CDP websocket.

    Returns:
        Tuple ``(h_v3_object, controller_index)`` — the
        configurator scope object handle and the controller slot.

    Raises:
        RuntimeError: Any step fails to locate its target.
    """
    on_click = await _click_handler_object_id(websocket, 1000)
    scopes = await _scopes_object_id(websocket, on_click, 1001)
    scope1 = await _scope1_object_id(websocket, scopes, 1002)
    h_obj = await _scope1_h_object_id(websocket, scope1, 1003)
    return await _h_v3_object_id(websocket, h_obj, 1004)
async def preview_popup_config(
    websocket: aiohttp.ClientWebSocketResponse,
    h_v3_object: str,
    appid: int,
    controller_index: int,
    config_url: str,
    *,
    msg_id: int,
) -> None:
    """Trigger a non-destructive preview of one controller-config URL.

    Calls ``EnsureEditingConfiguration`` then
    ``ApplyConfigurationFromURL`` on the configurator.

    Args:
        websocket: Popup CDP websocket.
        h_v3_object: Configurator scope handle.
        appid: AppID to preview the config for.
        controller_index: Controller slot index.
        config_url: Template URL (e.g. ``template://controller_neptune_*.vdf``).
        msg_id: CDP message ID.
    """
    await cdp_command(
        websocket,
        msg_id,
        "Runtime.callFunctionOn",
        {
            "objectId": h_v3_object,
            "functionDeclaration": (
                "function(appid, controllerIndex, url){ "
                "this.PreviewConfigForApp(appid, controllerIndex, url); "
                "return true; "
                "}"
            ),
            "arguments": [
                {"value": appid},
                {"value": controller_index},
                {"value": config_url},
            ],
            "awaitPromise": True,
            "returnByValue": True,
        },
    )

async def set_active_popup_config(
    websocket: aiohttp.ClientWebSocketResponse,
    h_v3_object: str,
    appid: int,
    controller_index: int,
    config_url: str,
    *,
    msg_id: int,
) -> None:

    """Save one controller-config URL as the active config for an AppID.

    Calls ``SetActiveConfigForApp`` + ``SaveEditingConfiguration``
    and clears any cached selection — the new config persists
    across Steam restarts.

    Args:
        websocket: Popup CDP websocket.
        h_v3_object: Configurator scope handle.
        appid: AppID to apply the config to.
        controller_index: Controller slot index.
        config_url: Template URL to save as active.
        msg_id: CDP message ID.
    """
    await cdp_command(
        websocket,
        msg_id,
        "Runtime.callFunctionOn",
        {
            "objectId": h_v3_object,
            "functionDeclaration": (
                "function(appid, controllerIndex, url){ "
                "this.SetActiveConfigForApp(appid, controllerIndex, url, false); "
                "this.SaveEditingConfiguration(appid); "
                "if (typeof this.ClearSelectedConfigCache === 'function') { "
                "    this.ClearSelectedConfigCache(appid); "
                "} "
                "this.EnsureEditingConfiguration(appid, controllerIndex); "
                "return true; "
                "}"
            ),
            "arguments": [
                {"value": appid},
                {"value": controller_index},
                {"value": config_url},
            ],
            "awaitPromise": True,
            "returnByValue": True,
        },
    )

async def inspect_popup_state(
    websocket: aiohttp.ClientWebSocketResponse,
    *,
    msg_id: int,
) -> dict[str, Any]:

    """Sample the current popup state (active template URL + title).

    Walks the React fiber tree from a heuristic-matched
    summary element to its ``props.config`` and serializes
    the interesting fields.

    Args:
        websocket: Popup CDP websocket.
        msg_id: CDP message ID.

    Returns:
        Dict with ``body``, ``title``, ``url``, ``progenitor``,
        ``usesMouse``, ``usesKeyboard``, ``usesGamepad``.
    """
    state_resp = await cdp_command(
        websocket,
        msg_id,
        "Runtime.evaluate",
        {
            "expression": r"""(() => {
                const node = Array.from(
                    document.querySelectorAll('button,[role="link"]')
                ).find((element) => (
                    (element.textContent || '').includes('Official Layout for') ||
                    (element.textContent || '').includes('Using Template:') ||
                    (element.textContent || '').includes('Gamepad With Joystick Trackpad') ||
                    (element.textContent || '').includes('Keyboard (WASD) and Mouse')
                ));
                const fiberKey = node ? Object.keys(node).find(
                    (key) => key.startsWith('__reactFiber')
                ) : null;
                let fiber = fiberKey ? node[fiberKey] : null;
                let config = null;
                while (fiber) {
                    const props = fiber.memoizedProps || {};
                    if (props.config && typeof props.config === 'object') {
                        config = props.config;
                        break;
                    }
                    fiber = fiber.return;
                }
                return {
                    body: document.body
                        ? document.body.innerText.slice(0, 1200)
                        : null,
                    title: config?.Title || null,
                    url: config?.URL || null,
                    progenitor: config?.ProgenitorURL || null,
                    usesMouse: config?.bUsesMouse ?? null,
                    usesKeyboard: config?.bUsesKeyboard ?? null,
                    usesGamepad: config?.bUsesGamepad ?? null,
                };
            })()""",
            "awaitPromise": True,
            "returnByValue": True,
            "userGesture": True,
        },
    )
    value = state_resp.get("result", {}).get("result", {}).get("value")
    return value if isinstance(value, dict) else {}