"""POSIX signal handling and game process registry for clean termination cascades."""

from __future__ import annotations
import logging
import os
import signal
import subprocess
from dataclasses import dataclass, field
logger = logging.getLogger(__name__)
CLEANUP_PATTERNS = (
    "steam-runtime-launch-client",
    "umu-run",
)
@dataclass
class SignalState:
    """Mutable state shared between the registry and the signal handler.

    Attributes:
        terminated_by_signal: Flipped to True when SIGTERM /
            SIGINT cascade fires; lets the launcher distinguish
            user-cancellation from a clean exit.
        pending_pids: PIDs currently tracked by the registry.
    """
    terminated_by_signal: bool = False
    pending_pids: set[int] = field(default_factory=set)
class GameProcessRegistry:
    """Track child PIDs and broadcast SIGTERM on shutdown.

    Used by the launcher to keep tabs on every spawned game/
    wrapper process so we can cascade SIGTERM through the
    process group and clean up known wrapper patterns
    (``steam-runtime-launch-client``, ``umu-run``) on exit.
    """
    def __init__(self, state: SignalState) -> None:
        """Initialize the instance."""
        self._state = state
    def track(self, proc: subprocess.Popen) -> None:
        """Add a subprocess PID to the pending set for later cleanup.

        Args:
            proc: Live ``subprocess.Popen``-like object.
        """
        if proc.pid:
            self._state.pending_pids.add(proc.pid)
    def untrack(self, proc: subprocess.Popen) -> None:
        """Remove a subprocess PID from the pending set.

        Args:
            proc: ``subprocess.Popen``-like object.
        """
        self._state.pending_pids.discard(proc.pid)
    def terminate_all(self) -> None:
        """Cascade SIGTERM to every tracked PID's process group.

        Steps: flag ``terminated_by_signal`` → for each PID, try
        ``killpg`` then fall back to ``kill`` → run ``pkill -TERM``
        against the well-known wrapper patterns.

        Process-already-gone and permission errors are swallowed.
        """
        self._state.terminated_by_signal = True
        for pid in list(self._state.pending_pids):
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            except OSError:
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
        for pattern in CLEANUP_PATTERNS:
            try:
                subprocess.run(
                    ["pkill", "-TERM", "-f", pattern],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

def install_signal_handlers(
    registry: GameProcessRegistry,
) -> SignalState:

    """Wire SIGTERM / SIGINT to ``registry.terminate_all``.

    Args:
        registry: The process registry to drive on signal.

    Returns:
        The ``SignalState`` shared with the registry.
    """
    state = registry._state
    def _handler(signum: int, _frame: object | None) -> None:
        """Inner closure registered as the SIGTERM/SIGINT handler.

        Logs the signal and triggers the cascade by calling
        ``registry.terminate_all``.

        Args:
            signum: Signal number (received by Python's handler
                machinery).
            _frame: Stack frame at the point of interruption
                (unused).
        """
        logger.info(
            "[launcher.signals] received signal %d, terminating games",
            signum,
        )
        registry.terminate_all()
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
    return state