"""Backend-side ``unifideck://`` URI dispatcher.

OP-22b | py_modules/unifideck/actions/dispatch.py

Routes a parsed action to the right backend handler.
This is the function-style equivalent of
``ActionHandlers.dispatch_unifideck_action`` (OP-25b) — the
mixin-style ``ActionRPCMixin`` (OP-26a) calls into here.

Verb routing happens via a short if-chain rather than a
dispatch table because each handler has slightly different
async/sync semantics and dependency requirements; the
explicit chain reads more clearly than a uniform table.

Every handler that depends on an optional service
(``cloudsave``, ``sync_service``) raises
``service_unavailable`` rather than crashing when the
service is missing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from unifideck.actions.unifideck_uri import SCOPE_FRONTEND, parse_unifideck_uri
from unifideck.rpc import RpcError

if TYPE_CHECKING:
    from unifideck.core.sync_service import SyncService
    from unifideck.services.cloud_save import CloudSaveService
    from unifideck.stores import StoreRegistry

logger = logging.getLogger(__name__)


async def dispatch_backend_action(
    *,
    uri: str,
    registry: StoreRegistry,
    cloudsave: CloudSaveService | None,
    sync_service: SyncService | None
) -> Any:
    """Parse ``uri`` and dispatch to the matching backend verb handler.

    Three-step pipeline:

    1. ``parse_unifideck_uri`` → typed ``ParsedAction``.
    2. Reject invalid URIs (``invalid_uri``) and
       frontend-scope verbs (``frontend_scope_verb``).
    3. Route on ``action.verb`` to the matching
       private handler.

    Unknown verbs raise ``unhandled_backend_verb`` with
    a hint pointing back at this function — a clear
    signal that adding a verb requires adding a branch
    here.

    Keyword-only arguments make the call site explicit
    about which collaborator goes where (the parameter
    list is long enough that positional confusion is a
    real risk).

    Args:
        uri: the ``unifideck://...`` URI.
        registry: store registry, used by ``auth`` verb.
        cloudsave: optional cloud-save service, used by
            ``retry-sync``.
        sync_service: optional sync service, used by
            ``refresh-*`` verbs.

    Returns:
        Per-verb result dict.

    Raises:
        RpcError: on parse failure, frontend-scope verb,
            unknown verb, missing optional service.
    """
    action = parse_unifideck_uri(uri)
    if not action.valid:
        raise RpcError("invalid_uri", reason=action.error, uri=uri)
    if action.scope == SCOPE_FRONTEND:
        raise RpcError(
            "frontend_scope_verb",
            verb=action.verb,
            hint="frontend should handle settings/* locally",
        )
    if action.verb == "auth":
        return await _dispatch_auth(action, registry)
    if action.verb == "retry-sync":
        return await _dispatch_retry_sync(action, cloudsave)
    if action.verb == "refresh-library":
        return _dispatch_refresh_library(action, sync_service)
    if action.verb == "refresh-all-libraries":
        return _dispatch_refresh_all(sync_service)
    raise RpcError(
        "unhandled_backend_verb",
        verb=action.verb,
        hint="add a handler in dispatch_backend_action",
    )


async def _dispatch_auth(action: Any, registry: StoreRegistry) -> Any:
    """Forward ``auth/<store>`` to ``registry.auth_action(store, "start")``.

    Args:
        action: parsed action; ``args[0]`` is the store id.
        registry: store registry.

    Returns:
        Auth-action result dict from the registry.
    """
    store = action.args[0]
    return await registry.auth_action(store, "start")


async def _dispatch_retry_sync(action: Any, cloudsave: CloudSaveService | None) -> dict[str, Any]:
    """Handle ``retry-sync/<store>/<game_id>/<phase>``.

    Phase must be ``"sync_down"`` or ``"sync_up"`` —
    anything else raises ``invalid_phase`` with the
    allowed list in the error context.

    The returned dict flattens the cloud-save service's
    ``Result`` with the request context for the frontend
    (which needs to know which game / phase failed).

    Args:
        action: parsed action with three args.
        cloudsave: cloud-save service; ``None`` raises
            ``service_unavailable``.

    Returns:
        ``{success, error, store, game_id, phase}`` dict.

    Raises:
        RpcError: ``service_unavailable`` or
            ``invalid_phase``.
    """
    if cloudsave is None:
        raise RpcError("service_unavailable", service="cloudsave")
    store, game_id, phase = action.args
    if phase == "sync_down":
        result = await cloudsave.sync_down(store, game_id)
    elif phase == "sync_up":
        result = await cloudsave.sync_up(store, game_id)
    else:
        raise RpcError(
            "invalid_phase",
            phase=phase,
            supported=["sync_down", "sync_up"],
        )
    return {
        "success": result.success,
        "error": result.error,
        "store": store,
        "game_id": game_id,
        "phase": phase,
    }


def _dispatch_refresh_library(action: Any, sync_service: SyncService | None) -> dict[str, Any]:
    """Handle ``refresh-library/<store>`` — fire-and-forget background sync.

    Spawns the sync as an asyncio task with a named
    identifier (``refresh-library-<store>``) for
    diagnostics and returns immediately with
    ``status="scheduled"`` so the RPC caller isn't
    blocked.

    Args:
        action: parsed action; ``args[0]`` is the store id.
        sync_service: sync service; ``None`` raises
            ``service_unavailable``.

    Returns:
        ``{success: True, store, status: "scheduled"}``.
    """
    if sync_service is None:
        raise RpcError("service_unavailable", service="sync_service")
    store = action.args[0]
    asyncio.create_task(sync_service.sync_single_store(store), name=f"refresh-library-{store}")
    return {"success": True, "store": store, "status": "scheduled"}


def _dispatch_refresh_all(sync_service: SyncService | None) -> dict[str, Any]:
    """Handle ``refresh-all-libraries`` — fire-and-forget full sync.

    Same fire-and-forget pattern as
    ``_dispatch_refresh_library`` but uses
    ``sync_service.sync_all()``.

    Args:
        sync_service: sync service; ``None`` raises
            ``service_unavailable``.

    Returns:
        ``{success: True, status: "scheduled"}``.
    """
    if sync_service is None:
        raise RpcError("service_unavailable", service="sync_service")
    asyncio.create_task(sync_service.sync_all(), name="refresh-all-libraries")
    return {"success": True, "status": "scheduled"}
