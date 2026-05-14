"""Phase timing telemetry — emits LAUNCH_PHASE_TIMING events for each launch milestone."""

from __future__ import annotations
import logging
import time
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    from ...event_bus.event_bus import EventBus
from .correlation import get_launch_id
logger = logging.getLogger(__name__)
LAUNCH_PHASE_TIMING_EVENT = "LAUNCH_PHASE_TIMING"
class PhaseTimer:
    """Async context manager that emits a LAUNCH_PHASE_TIMING event on exit.

    Records the wall-clock duration of the wrapped block and
    emits ``{phase, duration_ms, launch_id, success, **extra}``
    regardless of whether the block raised. Emit failures are
    swallowed (logged as exceptions).

    Attributes:
        None publicly — internal state only.
    """
    __slots__ = ("_bus", "_phase", "_extra", "_t0")
    def __init__(
        self,
        bus: EventBus,
        phase: str,
        extra: dict | None = None,
    ) -> None:
        """Capture the event bus, phase name, and any extra payload.

        Args:
            bus: Event bus (may be ``None`` to disable emission).
            phase: Phase identifier (free-form string).
            extra: Optional extra payload merged into the event
                (copied defensively).
        """
        self._bus = bus
        self._phase = phase
        self._extra = dict(extra) if extra else {}
        self._t0 = 0.0
    async def __aenter__(self) -> PhaseTimer:
        """Start the timer.

        Returns:
            Self.
        """
        self._t0 = time.monotonic()
        return self
    async def __aexit__(
        self, exc_type: Any, _exc_val: Any, _exc_tb: Any,
    ) -> None:
        """Stop the timer and emit the LAUNCH_PHASE_TIMING event.

        Args:
            exc_type: Exception type if the block raised, else ``None``.
            _exc_val: Unused.
            _exc_tb: Unused.
        """
        duration_ms = int((time.monotonic() - self._t0) * 1000)
        payload = {
            "phase": self._phase,
            "duration_ms": duration_ms,
            "launch_id": get_launch_id(),
            "success": exc_type is None,
            **self._extra,
        }
        if self._bus is not None:
            try:
                await self._bus.emit(
                    LAUNCH_PHASE_TIMING_EVENT, **payload,
                )
            except Exception:
                logger.exception(
                    "[telemetry] emit failed for phase=%s",
                    self._phase,
                )
        logger.debug(
            "[telemetry] phase=%s duration_ms=%d success=%s",
            self._phase, duration_ms, exc_type is None,
        )