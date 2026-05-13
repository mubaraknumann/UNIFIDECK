"""Shared helpers for store CLIs (legendary, gogdl, nile) install flows.

OP-25-shared-cli-install
File: py_modules/unifideck/stores/shared/cli_install_helpers.py

Each store's install handler spawns its own CLI as
a subprocess and needs three boilerplate behaviours:

* **Drain output** — read stdout line by line and
  feed each line to a store-specific parser
  (different CLIs format progress differently);
* **Wait with timeout** — bound the process at e.g.
  3 hours; on timeout kill and return -1 so the
  caller can map it to a domain error;
* **Parse progress** — extract the floating-point
  percentage from a matched line using a
  caller-supplied regex.

Factoring these here avoids duplicating the pattern
across epic/, gog/, amazon/ install modules.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import re
    from collections.abc import Awaitable, Callable

    LineHandler = Callable[[str, str, "ProgressCallback | None"], Awaitable[None]]
    ProgressCallback = Callable[[float], Awaitable[None]]

logger = logging.getLogger(__name__)


async def drain_install_output(proc: Any, game_id: str, progress_cb: ProgressCallback | None, line_handler: LineHandler) -> None:
    """Read every line from ``proc.stdout`` and forward to ``line_handler``.

    Loops until the pipe returns an empty bytes
    (signaling EOF / process exit). Each line is
    decoded leniently (``errors="ignore"`` so a
    spurious non-UTF byte doesn't crash the drain),
    stripped, and only forwarded if non-empty
    (blank lines from progress redraws would be
    noise).

    Args:
        proc: ``asyncio.subprocess.Process`` with
            ``stdout`` set.
        game_id: store-side game id, passed
            through to ``line_handler`` for log
            context.
        progress_cb: optional callback for
            progress percentages.
        line_handler: store-specific parser that
            extracts progress info and calls
            ``progress_cb`` when applicable.
    """
    assert proc.stdout is not None
    while True:
        line_bytes = await proc.stdout.readline()
        if not line_bytes:
            break
        line = line_bytes.decode(errors="ignore").strip()
        if line:
            await line_handler(line, game_id, progress_cb)


async def wait_with_timeout(proc: Any, timeout_s: int, log_prefix: str) -> int:
    """Await process exit with a hard timeout; on timeout, kill and return -1.

    On normal exit: returns ``proc.returncode`` (or
    ``0`` if ``None``, which should be unreachable
    after ``wait()`` but is a defensive fallback).

    On timeout: logs at ERROR with the ``log_prefix``
    so the caller's store name appears in the log,
    calls ``proc.kill()`` and ``await proc.wait()``
    to harvest the process so it doesn't zombie,
    returns ``-1`` as a sentinel.

    Args:
        proc: subprocess to await.
        timeout_s: timeout in seconds.
        log_prefix: short tag (e.g. ``"[epic]"``)
            included in timeout log.

    Returns:
        Subprocess return code, or ``-1`` on
        timeout.
    """
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except TimeoutError:
        logger.error(
            "%s timeout after %ds, killing",
            log_prefix,
            timeout_s,
        )
        proc.kill()
        await proc.wait()
        return -1
    return proc.returncode or 0


def parse_progress_line(line: str, pattern: re.Pattern[str]) -> float | None:
    """Apply ``pattern`` to ``line`` and return the captured percentage as float.

    The caller supplies the regex (each CLI has its
    own format — legendary uses
    ``Progress: 42.5%``, gogdl uses
    ``[42.5%]``, etc). The first capture group must
    be the percentage as text.

    Returns ``None`` on:

    * No match (line wasn't a progress line);
    * Match but the captured group can't be parsed
      as float (malformed output).

    Args:
        line: stripped output line.
        pattern: caller's compiled regex.

    Returns:
        Percentage as float, or ``None``.
    """
    match = pattern.search(line)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None
