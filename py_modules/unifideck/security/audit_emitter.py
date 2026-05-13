"""Free-function emitters for ``SECURITY_*`` audit events.

OP-11b | py_modules/unifideck/security/audit_emitter.py

Alternative to ``audit_decorators`` for callers that
prefer explicit emit calls over decoration. Each
function wraps the same ``_safe_emit`` plumbing:

* No-op if ``bus`` is ``None``;
* Schedule the emit as an asyncio task (fire-and-forget)
  so the audit doesn't block the audited operation;
* Skip silently when not in an event loop (defensive —
  unit-test invocations from sync code shouldn't crash).

Event-specific helpers cover the common cases:

* ``emit_auth_started`` / ``_completed`` / ``_failed`` —
  the auth-flow trio;
* ``emit_token_file_migrated`` —
  legacy-plaintext-rewrite signal;
* ``emit_legacy_plaintext_detected`` — discovery without
  rewrite;
* ``emit_permissions_check`` — every chmod check;
* ``emit_external_auth_check_failed`` — degraded
  state when the external service (e.g. accountsd)
  rejects a check.

Also re-exports a ``audit_auth_flow`` decorator that
delegates to ``emit_auth_*`` rather than the
``_emit_audit`` path in ``audit_decorators``. The two
decorators exist for historical reasons (different
import paths called the same shape) — they're kept
both for backward compatibility.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)


def _safe_emit(bus: EventBus, event_name: str, **kwargs: Any) -> None:
    """Schedule a bus emit as a background task, swallowing every failure.

    Three-arm safety:

    * ``bus is None`` → no-op (audit disabled);
    * ``get_running_loop`` raises ``RuntimeError``
      (no loop) → no-op (sync context, e.g. tests);
    * Any other exception during resolve / schedule
      → DEBUG log + swallow.

    The emit itself is scheduled as a named task
    (``audit-emit-<event>``) for diagnostic visibility
    in the task list.

    Args:
        bus: live ``EventBus`` or ``None``.
        event_name: ``Events`` member name.
        **kwargs: event payload.
    """
    if bus is None:
        return
    try:
        import asyncio
        from ..core.types.events import Events

        event = getattr(Events, event_name)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            bus.emit(event, **kwargs),
            name=f"audit-emit-{event_name}",
        )
    except Exception as e:
        logger.debug(
            "[audit_emitter] failed to emit %s: %s",
            event_name,
            e,
        )


def emit_auth_started(bus: EventBus, store: str, method: str | None = None) -> None:
    """Fire ``SECURITY_AUTH_FLOW_STARTED`` with store + optional method.

    ``method`` is omitted from the kwargs when ``None``
    so receivers don't see a stray key — keeps the
    event shape minimal.

    Args:
        bus: event bus.
        store: store identifier.
        method: optional auth method (``"oauth"``,
            ``"cdp"``, …).
    """
    kwargs = {"store": store}
    if method is not None:
        kwargs["method"] = method
    _safe_emit(bus, "SECURITY_AUTH_FLOW_STARTED", **kwargs)


def emit_auth_completed(
    bus: EventBus,
    store: str,
    duration_seconds: float | None = None,
    method: str | None = None,
) -> None:
    """Fire ``SECURITY_AUTH_FLOW_COMPLETED`` with timing.

    ``duration_seconds`` is rounded to 3 decimal places
    (millisecond precision) to keep the event payload
    deterministic across runs.

    Args:
        bus: event bus.
        store: store identifier.
        duration_seconds: total auth time; omitted when
            ``None``.
        method: optional method tag.
    """
    kwargs: dict[str, Any] = {"store": store}
    if duration_seconds is not None:
        kwargs["duration_seconds"] = round(duration_seconds, 3)
    if method is not None:
        kwargs["method"] = method
    _safe_emit(bus, "SECURITY_AUTH_FLOW_COMPLETED", **kwargs)


def emit_auth_failed(
    bus: EventBus,
    store: str,
    reason: str,
    duration_seconds: float | None = None,
    method: str | None = None,
) -> None:
    """Fire ``SECURITY_AUTH_FLOW_FAILED`` with reason + timing.

    ``reason`` is mandatory (the failure has to have
    some classification, even if just
    ``"unknown"``); duration and method follow the
    optional-omit pattern.

    Args:
        bus: event bus.
        store: store identifier.
        reason: machine-readable failure code.
        duration_seconds: total elapsed time.
        method: optional method tag.
    """
    kwargs: dict[str, Any] = {"store": store, "reason": reason}
    if duration_seconds is not None:
        kwargs["duration_seconds"] = round(duration_seconds, 3)
    if method is not None:
        kwargs["method"] = method
    _safe_emit(bus, "SECURITY_AUTH_FLOW_FAILED", **kwargs)


def emit_token_file_migrated(
    bus: EventBus,
    store: str,
    from_path: str,
    to_path: str,
) -> None:
    """Fire ``SECURITY_TOKEN_FILE_MIGRATED`` for legacy → encrypted rewrites.

    Emitted exactly once per migration (the token
    store tracks state to avoid duplicate emits).
    Carries both paths so consumers can audit the
    move.

    Args:
        bus: event bus.
        store: store identifier.
        from_path: original (legacy) path.
        to_path: new encrypted-format path.
    """
    _safe_emit(
        bus,
        "SECURITY_TOKEN_FILE_MIGRATED",
        store=store,
        from_path=from_path,
        to_path=to_path,
    )


def emit_legacy_plaintext_detected(bus: EventBus, store: str, path: str) -> None:
    """Fire ``SECURITY_LEGACY_PLAINTEXT_DETECTED`` on read of an unmigrated file.

    Used when the token store finds an old plaintext
    token file but hasn't (yet) rewritten it —
    typically because we're in a read-only context.
    Consumers may surface a UI warning telling the
    user to log in again.

    Args:
        bus: event bus.
        store: store identifier.
        path: path of the legacy file.
    """
    _safe_emit(
        bus,
        "SECURITY_LEGACY_PLAINTEXT_DETECTED",
        store=store,
        path=path,
    )


def emit_permissions_check(bus: EventBus, store: str, path: str, mode: int) -> None:
    """Fire ``SECURITY_PERMISSIONS_CHECK`` for every chmod check.

    Carries the observed mode (POSIX permission bits)
    so observers can detect drift from the expected
    ``0o600``.

    Args:
        bus: event bus.
        store: store identifier.
        path: file whose mode was checked.
        mode: observed mode as an integer (octal bits).
    """
    _safe_emit(
        bus,
        "SECURITY_PERMISSIONS_CHECK",
        store=store,
        path=path,
        mode=mode,
    )


def emit_external_auth_check_failed(
    bus: EventBus,
    store: str,
    reason: str,
    detail: str = "",
) -> None:
    """Fire ``SECURITY_EXTERNAL_AUTH_CHECK_FAILED`` for degraded auth checks.

    Emitted when the plugin couldn't reach an
    external auth verifier (e.g. accountsd) and is
    falling back to a degraded check. ``detail`` is
    truncated to 64 chars to keep the event payload
    bounded.

    Args:
        bus: event bus.
        store: store identifier.
        reason: machine-readable code.
        detail: free-form additional context.
    """
    kwargs = {"store": store, "reason": reason}
    if detail:
        kwargs["detail"] = str(detail)[:64]
    _safe_emit(
        bus,
        "SECURITY_EXTERNAL_AUTH_CHECK_FAILED",
        **kwargs,
    )


def audit_auth_flow(store: str, method: str = "oauth") -> Callable:
    """Decorator factory — auth-flow wrap using the emit_* free functions.

    Functionally equivalent to
    ``audit_decorators.audit_auth_flow`` but delegates
    via the typed emit functions defined above (slightly
    nicer payloads with rounded durations).

    Result attribute inspection: if the decorated
    method returns a ``Result``-like object, the
    decorator inspects ``result.success`` to decide
    completed-vs-failed and walks the typed error
    attributes (``error``, ``error_code``, ``reason``,
    ``message``) to extract a reason.

    Args:
        store: store identifier.
        method: auth method (default ``"oauth"``).

    Returns:
        Decorator function.
    """

    def decorator(target: Callable) -> Callable:
        """Inner decorator producing the wrapped coroutine.

        Args:
            target: coroutine method to wrap.

        Returns:
            Wrapped coroutine.
        """

        @functools.wraps(target)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            """Run ``target`` between started + completed/failed emits.

            Bypass: if no bus on ``self``, skip every
            emit and just call through (zero-cost
            audit-off mode).

            Args:
                self: bound instance.
                *args / **kwargs: forwarded.

            Returns:
                Whatever ``target`` returns.

            Raises:
                Whatever ``target`` raises.
            """
            bus = getattr(self, "_bus", None)
            if bus is None:
                return await target(self, *args, **kwargs)
            emit_auth_started(bus, store, method=method)
            t0 = time.monotonic()
            try:
                result = await target(self, *args, **kwargs)
            except Exception as e:
                _emit_flow_outcome(
                    bus,
                    store,
                    method,
                    False,
                    time.monotonic() - t0,
                    type(e).__name__,
                )
                raise
            _emit_flow_outcome(
                bus,
                store,
                method,
                bool(getattr(result, "success", False)),
                time.monotonic() - t0,
                _extract_failure_reason(result),
            )
            return result

        return wrapper

    return decorator


def _emit_flow_outcome(
    bus: EventBus,
    store: str,
    method: str,
    success: bool,
    duration: float,
    failure_reason: str,
) -> None:
    """Dispatch to ``emit_auth_completed`` or ``emit_auth_failed`` based on outcome.

    Args:
        bus: event bus.
        store: store identifier.
        method: auth method.
        success: True for completed, False for failed.
        duration: elapsed monotonic time in seconds.
        failure_reason: reason string (only used in
            failure path).
    """
    if success:
        emit_auth_completed(bus, store, duration, method=method)
    else:
        emit_auth_failed(
            bus,
            store,
            failure_reason,
            duration,
            method=method,
        )


def _extract_failure_reason(result: Any) -> str:
    """Walk a result object's typed error fields and return the first non-empty one.

    Search order: ``error`` → ``error_code`` →
    ``reason`` → ``message``. Falls back to
    ``"unknown"`` if nothing matched. Truncates to 64
    chars to keep the event payload bounded.

    Args:
        result: typed result object.

    Returns:
        Reason string (max 64 chars).
    """
    for attr in ("error", "error_code", "reason", "message"):
        val = getattr(result, attr, None)
        if val:
            return str(val)[:64]
    return "unknown"
