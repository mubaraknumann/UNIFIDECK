"""Decorators emitting ``SECURITY_*`` audit events around operations.

OP-11a | py_modules/unifideck/security/audit_decorators.py

Two decorator factories for non-invasive audit wiring:

* ``@audit_auth_flow(store, method)`` — wraps a coroutine
  method so each invocation emits
  ``SECURITY_AUTH_FLOW_STARTED`` on entry,
  ``SECURITY_AUTH_FLOW_FAILED`` on exception (with
  exception class + duration), and
  ``SECURITY_AUTH_FLOW_COMPLETED`` on success (with
  duration).
* ``@audit_token_op(operation, store)`` — handles the
  token-migration case where the operation's result
  string is the new file path; emits
  ``SECURITY_TOKEN_FILE_MIGRATED`` only when the
  ``_migration_occurred`` flag was set on the instance.

Both decorators look up ``self._bus`` at call time (not
decoration time) so the bus can be injected after
construction. Missing bus silently skips the emit — the
decorators never raise.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def audit_auth_flow(store: str, method: str = "oauth") -> Callable:
    """Decorator factory wrapping a coroutine with auth-flow audit events.

    Captured at decoration time: ``store`` + ``method``
    pre-bind the event metadata so each invocation has
    its origin tagged.

    The wrapper measures wall-clock duration around the
    inner call and includes it in the
    completed/failed events so consumers can build
    latency histograms.

    Args:
        store: store identifier (e.g. ``"epic"``).
        method: auth method identifier
            (``"oauth"``, ``"cdp"``, …); default
            ``"oauth"``.

    Returns:
        Decorator function ready to apply to a
        coroutine method.
    """

    def decorator(func: Callable) -> Callable:
        """Build the async wrapper that emits the trio of events.

        Args:
            func: coroutine method to wrap.

        Returns:
            Wrapped coroutine.
        """

        @functools.wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            """Emit started, then run, emit failed-or-completed with duration.

            ``self._bus`` is read at call time so any
            constructor-time bus changes are picked up.
            The exception path re-raises after emitting
            failed; the success path emits completed
            and returns the result.

            Args:
                self: bound instance.
                *args / **kwargs: forwarded to ``func``.

            Returns:
                Whatever ``func`` returns.

            Raises:
                Whatever ``func`` raises (after audit).
            """
            bus = getattr(self, "_bus", None)
            started_at = time.time()
            _emit_audit(
                bus,
                "SECURITY_AUTH_FLOW_STARTED",
                store=store,
                method=method,
            )
            try:
                result = await func(self, *args, **kwargs)
            except Exception as exc:
                duration_ms = int((time.time() - started_at) * 1000)
                _emit_audit(
                    bus,
                    "SECURITY_AUTH_FLOW_FAILED",
                    store=store,
                    method=method,
                    reason=type(exc).__name__,
                    duration_ms=duration_ms,
                )
                raise
            duration_ms = int((time.time() - started_at) * 1000)
            _emit_audit(
                bus,
                "SECURITY_AUTH_FLOW_COMPLETED",
                store=store,
                method=method,
                duration_ms=duration_ms,
            )
            return result

        return wrapper

    return decorator


def audit_token_op(operation: str, store: str) -> Callable:
    """Decorator factory for token-operation audit (migration-aware).

    Currently the only handled ``operation`` is
    ``"migrate"`` — when set, the wrapper inspects
    the inner result + the instance's
    ``_migration_occurred`` flag and emits
    ``SECURITY_TOKEN_FILE_MIGRATED`` when both are
    truthy. Other operations are passed through
    untouched (the decorator is a no-op for them).

    Args:
        operation: operation kind (``"migrate"``).
        store: store identifier.

    Returns:
        Decorator.
    """

    def decorator(func: Callable) -> Callable:
        """Wrap ``func`` with the migration-emit pipeline.

        Args:
            func: coroutine to wrap.

        Returns:
            Wrapped coroutine.
        """

        @functools.wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            """Run ``func``, then conditionally emit ``SECURITY_TOKEN_FILE_MIGRATED``.

            The emit branch fires only when:

            * ``operation == "migrate"``;
            * the inner result is a string (the new
              path);
            * the instance's ``_migration_occurred``
              flag is truthy.

            Args:
                self: bound instance.
                *args / **kwargs: forwarded.

            Returns:
                Whatever ``func`` returns.
            """
            bus = getattr(self, "_bus", None)
            result = await func(self, *args, **kwargs)
            if operation == "migrate" and isinstance(result, str):
                _maybe_emit_migration(bus, self, store, result)
            return result

        return wrapper

    return decorator


def _emit_audit(bus: Any, event_name: str, **kwargs: Any) -> None:
    """Resolve the ``Events`` enum value and emit on the bus.

    Defensive: missing bus is a no-op; any exception
    during emit (event not found, bus rejected the
    call) is logged at DEBUG and swallowed — audit
    failures should never crash the audited
    operation.

    Args:
        bus: event bus or ``None``.
        event_name: ``Events`` member name.
        **kwargs: forwarded as the event payload.
    """
    if bus is None:
        return
    try:
        from ..core.types.events import Events

        event = getattr(Events, event_name)
        bus.emit(event, **kwargs)
    except Exception as e:
        logger.debug(
            "[audit_decorators] failed to emit %s: %s",
            event_name,
            e,
        )


def _maybe_emit_migration(
    bus: Any,
    instance: Any,
    store: str,
    result_path: str,
) -> None:
    """Fire ``SECURITY_TOKEN_FILE_MIGRATED`` if the instance signalled it.

    Looks up ``instance._migration_occurred`` — the
    token store sets this flag to ``True`` when a
    legacy plaintext file was detected and rewritten
    to the encrypted format. After the emit, the
    flag is reset to False so the next call doesn't
    re-emit.

    The AttributeError catch on the reset handles
    instances where the flag is read-only or comes
    from a base class (defensive — shouldn't happen
    in normal use).

    Args:
        bus: event bus or ``None``.
        instance: the bound instance (token store).
        store: store identifier.
        result_path: new file path (event payload).
    """
    if bus is None:
        return
    flag = getattr(instance, "_migration_occurred", False)
    if not flag:
        return
    _emit_audit(
        bus,
        "SECURITY_TOKEN_FILE_MIGRATED",
        store=store,
        new_path=result_path,
    )
    try:
        instance._migration_occurred = False
    except AttributeError:
        pass
