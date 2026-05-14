"""Per-launch correlation ID via contextvars — threads a stable token through every log emitted during a launch."""

from __future__ import annotations
import contextvars
import logging
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
_LAUNCH_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "unifideck_launch_id",
    default="-",
)
def new_launch_id() -> str:
    """Generate a fresh 8-hex-char launch identifier.

    Returns:
        A new random launch ID string (``secrets.token_hex(4)``).
    """
    return secrets.token_hex(4)
def get_launch_id() -> str:
    """Return the launch ID bound to the current async context.

    Returns:
        The active launch ID, or ``"-"`` if none has been set.
    """
    return _LAUNCH_ID.get()
@contextmanager
def launch_id_scope(launch_id: str) -> Iterator[None]:
    """Bind a launch ID to the current async context for the block's duration.

    Restores the previous launch ID on exit.

    Args:
        launch_id: Launch ID to set on the contextvar.

    Yields:
        None.
    """
    token = _LAUNCH_ID.set(launch_id)
    try:
        yield
    finally:
        _LAUNCH_ID.reset(token)
class LaunchIdFilter(logging.Filter):
    """logging.Filter that injects ``launch_id`` onto every LogRecord.

    Allows ``%(launch_id)s`` to be used in a logging format string
    to thread the per-launch correlation ID through stderr output.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        """Attach the active launch ID to the record and let it through.

        Args:
            record: The log record being filtered.

        Returns:
            Always ``True`` — the filter doesn't drop records.
        """
        record.launch_id = get_launch_id()
        return True
def install_launch_id_logging(
    root_logger: logging.Logger | None = None,
) -> None:
    """Install ``LaunchIdFilter`` on the root logger (idempotent).

    Args:
        root_logger: Override the root logger (defaults to
            ``logging.getLogger()``).
    """
    logger = root_logger or logging.getLogger()
    for existing in logger.filters:
        if isinstance(existing, LaunchIdFilter):
            return
    logger.addFilter(LaunchIdFilter())