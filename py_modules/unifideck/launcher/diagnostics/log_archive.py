"""Persistent log archival — writes per-launch log files to disk for post-mortem inspection."""

from __future__ import annotations
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ...config import ConfigManager
logger = logging.getLogger(__name__)
def _resolve_archive_dir(config: ConfigManager | None) -> Path:
    """Resolve and create the per-launch archive directory.

    Reads ``logs.archive_path`` from config. Default
    ``~/.local/share/unifideck/launches``. Failure to create
    the directory is logged but not raised.

    Args:
        config: ConfigManager, or ``None`` (uses default).

    Returns:
        Absolute path (may not exist if mkdir failed).
    """
    if config is None or not hasattr(config, "get_str"):
        raw = "~/.local/share/unifideck/launches"
    else:
        raw = config.get_str(
            "logs.archive_path",
            "~/.local/share/unifideck/launches",
        )
    path = Path(os.path.expanduser(raw))
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        logger.warning(
            "[log_archive] failed to create %s: %s — archiving disabled",
            path, err,
        )
    return path
def _resolve_retention_seconds(config: ConfigManager | None) -> int:
    """Resolve the log-retention duration from config.

    Reads ``logs.retention_days``. Default 7 days.

    Args:
        config: ConfigManager, or ``None`` (uses default).

    Returns:
        Retention in seconds.
    """
    if config is None or not hasattr(config, "get_int"):
        return 7 * 24 * 3600
    return config.get_int("logs.retention_days", 7) * 24 * 3600
def prune_old_launches(config: ConfigManager | None) -> int:
    """Delete archived ``*.log`` files older than the configured retention.

    Failures (unlink / stat / readdir) are logged but not raised.

    Args:
        config: ConfigManager.

    Returns:
        Number of files successfully removed.
    """
    archive_dir = _resolve_archive_dir(config)
    if not archive_dir.is_dir():
        return 0
    cutoff = time.time() - _resolve_retention_seconds(config)
    removed = 0
    try:
        for entry in archive_dir.iterdir():
            if not entry.is_file() or entry.suffix != ".log":
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError as err:
                logger.warning(
                    "[log_archive] failed to prune %s: %s",
                    entry, err,
                )
    except OSError as err:
        logger.warning(
            "[log_archive] failed to scan %s: %s", archive_dir, err,
        )
    if removed > 0:
        logger.info(
            "[log_archive] pruned %d expired launch log(s)", removed,
        )
    return removed

def attach_launch_handler(
    launch_id: str, config: ConfigManager | None,
    *, min_level: int = logging.INFO,
) -> logging.Handler | None:

    """Attach a per-launch FileHandler to ``unifideck.launcher``.

    Args:
        launch_id: Correlation ID (used as the filename stem).
        config: ConfigManager (for archive dir).
        min_level: Minimum level captured to the file.

    Returns:
        The handler instance (pass to ``detach_launch_handler``),
        or ``None`` if the file couldn't be opened.
    """
    archive_dir = _resolve_archive_dir(config)
    path = archive_dir / f"{launch_id}.log"
    try:
        handler = logging.FileHandler(str(path), encoding="utf-8")
        handler.setLevel(min_level)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        ))
        logging.getLogger("unifideck.launcher").addHandler(handler)
        return handler
    except OSError as err:
        logger.warning(
            "[log_archive] failed to attach handler for %s: %s",
            path, err,
        )
        return None
def detach_launch_handler(handler: logging.Handler | None) -> None:
    """Remove and close a handler previously returned by ``attach_launch_handler``.

    Args:
        handler: The handler to detach, or ``None`` (no-op).
    """
    if handler is None:
        return
    try:
        logging.getLogger("unifideck.launcher").removeHandler(handler)
        handler.close()
    except Exception:
        logger.exception("[log_archive] detach handler failed")
def read_launch_logs(
    launch_id: str, config: ConfigManager | None,
    *, max_lines: int = 500,
) -> dict:
    """Read up to ``max_lines`` of the tail of one archived launch log.

    Each line is parsed for a level marker (``[ERROR]`` /
    ``[WARNING]`` / ``[DEBUG]``; everything else INFO).

    Args:
        launch_id: Correlation ID.
        config: ConfigManager.
        max_lines: Cap on lines returned (tail bias).

    Returns:
        Dict ``{exists, path, lines, total}``. ``lines`` is a
        list of ``{level, text}`` dicts, empty if the file is
        missing or unreadable.
    """
    archive_dir = _resolve_archive_dir(config)
    path = archive_dir / f"{launch_id}.log"
    result = {
        "exists": False, "path": str(path), "lines": [],
        "total": 0,
    }
    if not path.is_file():
        return result
    result["exists"] = True
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            raw = fh.readlines()
    except OSError as err:
        logger.warning("[log_archive] read %s failed: %s", path, err)
        return result
    result["total"] = len(raw)
    tail = raw[-max_lines:] if len(raw) > max_lines else raw
    parsed = []
    for line in tail:
        level = "INFO"
        if "[ERROR]" in line or "[CRITICAL]" in line:
            level = "ERROR"
        elif "[WARNING]" in line:
            level = "WARNING"
        elif "[DEBUG]" in line:
            level = "DEBUG"
        parsed.append({"level": level, "text": line.rstrip("\n")})
    result["lines"] = parsed
    return result

def export_launch_logs(
    launch_id: str, dest_path: str, config: ConfigManager | None,
) -> dict:

    """Copy one archived launch log to a user-chosen path.

    ``dest_path`` is expanded (``~``) and resolved relative to
    the user's home if not already absolute. Parent dirs are
    created.

    Args:
        launch_id: Correlation ID.
        dest_path: Destination path (absolute or relative to home).
        config: ConfigManager.

    Returns:
        Dict ``{success, dest_path, error}``. On failure,
        ``error`` is either ``"source_missing"`` or the OS
        error string.
    """
    import shutil
    archive_dir = _resolve_archive_dir(config)
    src = archive_dir / f"{launch_id}.log"
    if not src.is_file():
        return {
            "success": False,
            "error": "source_missing",
            "dest_path": None,
        }
    dst = Path(os.path.expanduser(dest_path))
    if not dst.is_absolute():
        dst = Path.home() / dst
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
    except OSError as err:
        logger.warning(
            "[log_archive] export %s → %s failed: %s",
            src, dst, err,
        )
        return {
            "success": False,
            "error": str(err),
            "dest_path": str(dst),
        }
    return {
        "success": True,
        "dest_path": str(dst),
        "error": None,
    }