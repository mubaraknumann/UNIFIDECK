"""Process lifecycle helpers — graceful kill + startup-crash detection.

OP-15c3 | py_modules/unifideck/auth/edge_browser/process_ops.py

Edge has two operational quirks the plugin works
around:

* It buffers cookies in memory and only writes them
  to disk on a clean shutdown — so a hard kill loses
  the auth cookies we just captured. Hence the
  ``_COOKIE_FLUSH_DELAY_S`` pause before SIGTERM.
* It can crash silently within the first 10 seconds
  if Gamescope's compositor isn't ready. Hence the
  poll loop that watches both ``proc.poll()`` and
  the CDP port.

Two-tier kill: SIGTERM (10 s grace) → SIGKILL (3 s
grace). Process-group signal preferred over per-pid
to kill Edge's helper processes too.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_TERM_TIMEOUT_S = 10
_KILL_TIMEOUT_S = 3
_COOKIE_FLUSH_DELAY_S = 1
_STARTUP_POLL_STEPS = 20
_STARTUP_POLL_INTERVAL_S = 0.5
_CRASH_LOG_TAIL_CHARS = 300


def graceful_kill(proc: subprocess.Popen[bytes] | None) -> None:
    """Stop Edge cleanly so cookies flush; force-kill on timeout.

    Four-step:

    1. ``time.sleep(1)`` — gives Edge a moment to
       finish writing cookies if we caught the
       redirect mid-write;
    2. Send SIGTERM to the process group (catches
       helpers);
    3. ``wait(10s)`` — typical clean shutdown;
    4. On timeout → SIGKILL via ``_force_kill``.

    ``None`` arg → no-op (safe to call when proc is
    unset). Errors during the kill log at DEBUG and
    are swallowed — there's nothing actionable we
    can do.

    Args:
        proc: ``Popen`` instance or ``None``.
    """
    if proc is None:
        return
    try:
        import time

        time.sleep(_COOKIE_FLUSH_DELAY_S)
        _signal_group_or_single(proc, signal.SIGTERM)
        proc.wait(timeout=_TERM_TIMEOUT_S)
        logger.info("[Edge] Auth browser closed (cookies flushed)")
    except subprocess.TimeoutExpired:
        _force_kill(proc)
    except Exception as e:
        logger.debug("[Edge] Auth browser kill error (non-fatal): %s", e)


def _signal_group_or_single(
    proc: subprocess.Popen[bytes],
    sig: int,
) -> None:
    """Send ``sig`` to the process group; fall back to single-process kill.

    Process-group signalling kills Edge's helper
    processes too (renderer, GPU, network service).
    Without it, those linger and may keep the CDP
    port occupied.

    Guards:

    * ``getpgid`` fails → fall back to per-pid;
    * The group is our own process group →
      fall back to per-pid (don't suicide).

    For SIGTERM → ``terminate``; for SIGKILL →
    ``kill``. Other signals are not used here.

    Args:
        proc: target ``Popen``.
        sig: signal number.
    """
    pgid = _safe_getpgid(proc.pid)
    if pgid is not None and pgid != os.getpgrp():
        os.killpg(pgid, sig)
    elif sig == signal.SIGTERM:
        proc.terminate()
    else:
        proc.kill()


def _safe_getpgid(pid: int) -> int | None:
    """``os.getpgid`` wrapper returning ``None`` on any failure.

    Args:
        pid: target pid.

    Returns:
        Process group id, or ``None``.
    """
    try:
        return os.getpgid(pid)
    except Exception:
        return None


def _force_kill(proc: subprocess.Popen[bytes]) -> None:
    """Send SIGKILL after SIGTERM timeout, wait briefly for reap.

    Last-resort path triggered by ``graceful_kill``
    when the SIGTERM grace expired. All failures
    swallowed — we're past the point where the
    caller can usefully recover.

    Args:
        proc: target ``Popen``.
    """
    logger.debug("[Edge] Auth browser didn't exit -- sending SIGKILL")
    try:
        _signal_group_or_single(proc, signal.SIGKILL)
        proc.wait(timeout=_KILL_TIMEOUT_S)
    except Exception:
        pass


async def wait_and_check_crash(
    proc: subprocess.Popen[bytes] | None,
    probe_cdp: Callable[[], bool],
    log_file: str,
) -> bool:
    """Poll for either a process exit or a CDP-port success.

    20 iterations × 500 ms = 10 second window. Each
    iteration:

    * If proc exited → log the stderr tail + return
      False (crash);
    * If ``probe_cdp()`` returns True → return True
      (alive + responsive);
    * Otherwise sleep + retry.

    Timeout (loop exhausted) → return True with a
    WARN log. The CDP port not responding doesn't
    necessarily mean Edge crashed — it might just
    be slow on first launch.

    ``probe_cdp`` is sync (small socket connect) but
    called via ``to_thread`` so it doesn't block
    the event loop.

    Args:
        proc: process to monitor.
        probe_cdp: callable returning whether CDP is
            responsive.
        log_file: path to the stderr log file (for
            crash tail).

    Returns:
        False on detected crash, True otherwise.
    """
    if proc is None:
        return False
    for _ in range(_STARTUP_POLL_STEPS):
        await asyncio.sleep(_STARTUP_POLL_INTERVAL_S)
        if proc.poll() is not None:
            _log_crash_tail(log_file)
            return False
        if await asyncio.to_thread(probe_cdp):
            return True
    logger.warning(
        "[Edge] Auth browser started but CDP port not responding after %ds",
        int(_STARTUP_POLL_STEPS * _STARTUP_POLL_INTERVAL_S),
    )
    return True


def _log_crash_tail(log_file: str) -> None:
    """Read the tail of Edge's stderr log + ERROR-log it for diagnostics.

    Truncates to the last
    ``_CRASH_LOG_TAIL_CHARS`` (300 chars) — enough
    to capture the typical crash trailer without
    flooding the log. Read errors are silently
    ignored (we still want the ERROR log even with
    no detail).

    Args:
        log_file: path to Edge's stderr log file.
    """
    err = ""
    try:
        with Path(log_file).open() as f:
            err = f.read()[:_CRASH_LOG_TAIL_CHARS]
    except Exception:
        pass
    logger.error(
        "[Edge] Auth browser crashed before CDP. stderr: %s",
        err,
    )
