"""Cloud-save error classification — maps exceptions to stable error codes emitted to the UI."""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING, Any
from ...core.types.events import Events
if TYPE_CHECKING:
    from ...config import ConfigManager
    from ...event_bus.event_bus import EventBus
logger = logging.getLogger(__name__)
def classify_cloud_error(err: BaseException) -> str:
    """Map an exception raised during cloud-save sync to a stable error code.

    Recognized codes: ``network_unreachable``, ``auth_expired``,
    ``forbidden``, ``quota_exceeded``, ``server_error``,
    ``timed_out``, ``disk_full``, ``permission_denied``,
    ``disk_space_low``, ``cancelled``. Falls back to ``unknown``
    for anything else.

    Args:
        err: The exception caught during a sync attempt.

    Returns:
        Stable error code string used by the UI for i18n lookup.
    """
    try:
        import aiohttp
    except ImportError:
        aiohttp = None
    if aiohttp is not None:
        if isinstance(err, aiohttp.ClientConnectionError):
            return "network_unreachable"
        if isinstance(err, aiohttp.ClientResponseError):
            status = getattr(err, "status", 0)
            if status == 401:
                return "auth_expired"
            if status == 403:
                return "forbidden"
            if status == 413:
                return "quota_exceeded"
            if 500 <= status < 600:
                return "server_error"
        if isinstance(err, aiohttp.ClientTimeout):
            return "timed_out"
    if isinstance(err, OSError):
        import errno
        if err.errno == errno.ENOSPC:
            return "disk_full"
        if err.errno in (errno.EACCES, errno.EPERM):
            return "permission_denied"
    try:
        from .disk_space import LowDiskSpaceError
        if isinstance(err, LowDiskSpaceError):
            return "disk_space_low"
    except ImportError:
        pass
    try:
        import asyncio
        if isinstance(err, asyncio.CancelledError):
            return "cancelled"
    except ImportError:
        pass
    return "unknown"

_DEFAULT_BEHAVIOR = "toast"
_VALID_BEHAVIORS = frozenset({"silent", "toast"})
def get_failure_behavior(config: ConfigManager | None, store: str) -> str:
    """Resolve the toast vs silent behavior for a given store from config.

    Reads ``cloud.failure_behavior.<store>`` with fallback to
    ``cloud.failure_behavior.default``. Unrecognized values
    fall back to ``"toast"`` with a warning.

    Args:
        config: ConfigManager, or ``None`` (uses defaults).
        store: Store identifier.

    Returns:
        ``"silent"`` or ``"toast"``.
    """
    if config is None or not hasattr(config, "get_str"):
        return _DEFAULT_BEHAVIOR
    key = f"cloud.failure_behavior.{store}"
    raw = config.get_str(key, "")
    if not raw:
        raw = config.get_str(
            "cloud.failure_behavior.default", _DEFAULT_BEHAVIOR,
        )
    if raw not in _VALID_BEHAVIORS:
        logger.warning(
            "[cloud_failure] invalid behavior %r for store %s, "
            "falling back to %r", raw, store, _DEFAULT_BEHAVIOR,
        )
        return _DEFAULT_BEHAVIOR
    return raw
async def handle_cloud_sync_failure(
    bus: EventBus,
    config: ConfigManager | None,
    *,
    phase: str,
    store: str,
    game_id: str,
    error: BaseException,
) -> None:
    """Log a cloud sync failure and optionally emit a UI toast.

    Always logs the failure with the classified error code.
    When the per-store behavior is ``"toast"``, also emits a
    LAUNCHER_STAGE event so the frontend renders a notification.

    Args:
        bus: Event bus.
        config: ConfigManager for behavior lookup.
        phase: ``"sync_down"`` or ``"sync_up"``.
        store: Store identifier.
        game_id: Per-store game identifier.
        error: The caught exception.
    """
    code = classify_cloud_error(error)
    behavior = get_failure_behavior(config, store)
    logger.error(
        "[cloud_failure] phase=%s store=%s game_id=%s "
        "error_code=%s behavior=%s error=%s",
        phase, store, game_id, code, behavior, error,
        exc_info=error,
    )
    if behavior == "silent":
        return
    await _emit_toast(bus, phase=phase, store=store, game_id=game_id, code=code)
async def _emit_toast(
    bus: EventBus, *,
    phase: str, store: str, game_id: str, code: str,
) -> None:
    """Build and emit the LAUNCHER_STAGE toast for a cloud-sync failure.

    Selects the i18n key based on phase, attaches the resolved
    actionable button (deep link to retry / open folder /
    open settings, when available for the error code).

    Args:
        bus: Event bus.
        phase: Sync phase (``"sync_down"`` or ``"sync_up"``).
        store: Store identifier.
        game_id: Per-store game identifier.
        code: Classified error code from ``classify_cloud_error``.
    """
    if bus is None:
        return
    i18n_key = (
        "toasts.launcher.cloudSyncDownFailed"
        if phase == "sync_down"
        else "toasts.launcher.cloudSyncUpFailed"
    )
    payload: dict[str, Any] = {
        "severity": "warning",
        "i18n_key": i18n_key,
        "i18n_params": {
            "store": store,
            "error_code": code,
            "error_i18n_key": f"cloudSync.error.{code}",
        },
        "duration_ms": 6000,
        "game_id": game_id,
        "store": store,
        "phase": phase,
    }
    resolved_action = _resolve_toast_action(
        code, store=store, game_id=game_id, phase=phase,
    )
    if resolved_action is not None:
        payload["action"] = resolved_action
    try:
        await bus.emit(Events.LAUNCHER_STAGE, **payload)
    except Exception:
        logger.exception("[cloud_failure] toast emit failed")

def _resolve_toast_action(
    code: str, *, store: str, game_id: str, phase: str,
) -> dict[str, str] | None:

    """Resolve the actionable button for a toast from ``_TOAST_ACTIONS``.

    Looks up the action template by error code and substitutes
    ``{store}``/``{game_id}``/``{phase}`` placeholders in any
    URL fields. Returns ``None`` if no action is configured for
    the code, or if a template variable is missing.

    Args:
        code: Stable error code.
        store: Store identifier.
        game_id: Per-store game identifier.
        phase: Sync phase.

    Returns:
        Action dict (``i18n_label_key`` + URLs), or ``None``.
    """
    action = _TOAST_ACTIONS.get(code)
    if action is None:
        return None
    ctx_vars = {"store": store, "game_id": game_id, "phase": phase}
    working: dict[str, str] = dict(action)
    for url_key in ("target_url", "fallback_url"):
        if url_key not in working:
            continue
        try:
            working[url_key] = working[url_key].format(**ctx_vars)
        except (KeyError, IndexError) as err:
            logger.warning(
                "[cloud_failure] action template error for "
                "code=%s key=%s: %s — dropping action",
                code, url_key, err,
            )
            return None
    return working
_TOAST_ACTIONS = {
    "disk_space_low": {
        "i18n_label_key": "toasts.actions.openStorageManager",
        "target_url": "steam://settings/storage",
        "fallback_url": "steam://settings",
    },
    "auth_expired": {
        "i18n_label_key": "toasts.actions.signInToStore",
        "target_url": "unifideck://auth/{store}",
    },
    "network_unreachable": {
        "i18n_label_key": "toasts.actions.retrySync",
        "target_url": "unifideck://retry-sync/{store}/{game_id}/{phase}",
    },
    "timed_out": {
        "i18n_label_key": "toasts.actions.retrySync",
        "target_url": "unifideck://retry-sync/{store}/{game_id}/{phase}",
    },
    "server_error": {
        "i18n_label_key": "toasts.actions.retrySync",
        "target_url": "unifideck://retry-sync/{store}/{game_id}/{phase}",
    },
    "unknown": {
        "i18n_label_key": "toasts.actions.openSaveFolder",
        "target_url": "unifideck://open-save-folder/{store}/{game_id}",
    },
    "permission_denied": {
        "i18n_label_key": "toasts.actions.openSaveFolder",
        "target_url": "unifideck://open-save-folder/{store}/{game_id}",
    },
    "cancelled": {
        "i18n_label_key": "toasts.actions.openSaveFolder",
        "target_url": "unifideck://open-save-folder/{store}/{game_id}",
    },
}