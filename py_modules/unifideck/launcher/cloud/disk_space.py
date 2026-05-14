"""Pre-launch disk space verification for cloud-save downloads."""

from __future__ import annotations
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast
if TYPE_CHECKING:
    from ...config import ConfigManager
logger = logging.getLogger(__name__)
_DEFAULT_MIN_FREE_BYTES = 1024 * 1024 * 1024
@dataclass(frozen=True)
class DiskSpaceCheck:
    """Outcome of a disk-space check.

    Attributes:
        has_space: True iff ``free_bytes >= required_bytes``.
        free_bytes: Free space on the mountpoint (0 on stat failure).
        required_bytes: Threshold the check used.
        path: The path actually probed (parent walk applied when
            the requested path didn't exist yet).
    """
    has_space: bool
    free_bytes: int
    required_bytes: int
    path: Path
class LowDiskSpaceError(Exception):
    """Raised when a pre-launch disk-space check fails the threshold.

    Attributes:
        free_bytes: Free space observed at fault time.
        required_bytes: Threshold that wasn't met.
    """
    def __init__(
        self,
        message: str,
        free_bytes: int,
        required_bytes: int,
    ) -> None:
        """Initialize with the failure message and the byte counts.

        Args:
            message: Human-readable explanation.
            free_bytes: Free space observed.
            required_bytes: Threshold.
        """
        super().__init__(message)
        self.free_bytes = free_bytes
        self.required_bytes = required_bytes
def get_min_free_bytes(config: ConfigManager | None) -> int:
    """Resolve the minimum-free-bytes threshold from config.

    Reads ``disk_space.min_free_bytes``. Default 1 GiB.

    Args:
        config: ConfigManager, or ``None`` (uses default).

    Returns:
        Threshold in bytes.
    """
    if config is None or not hasattr(config, "get_int"):
        return _DEFAULT_MIN_FREE_BYTES
    return config.get_int(
        "disk_space.min_free_bytes", _DEFAULT_MIN_FREE_BYTES,
    )
def check_disk_space(
    path: Path, required_bytes: int,
) -> DiskSpaceCheck:
    """Verify free disk space at ``path`` meets ``required_bytes``.

    Walks up to the first existing parent if ``path`` doesn't
    exist yet, then calls ``shutil.disk_usage``. A stat failure
    is treated as no-space (returns has_space=False).

    Args:
        path: Target path (or any subpath of an existing mount).
        required_bytes: Threshold.

    Returns:
        A ``DiskSpaceCheck``.
    """
    probe = path
    while probe != probe.parent and not probe.exists():
        probe = probe.parent
    try:
        usage = shutil.disk_usage(str(probe))
        return DiskSpaceCheck(
            has_space=usage.free >= required_bytes,
            free_bytes=usage.free,
            required_bytes=required_bytes,
            path=probe,
        )
    except OSError as err:
        logger.warning(
            "[disk_space] disk_usage(%s) failed: %s — assuming no space",
            probe, err,
        )
        return DiskSpaceCheck(
            has_space=False,
            free_bytes=0,
            required_bytes=required_bytes,
            path=probe,
        )

def assert_enough_space(
    path: Path, config: ConfigManager | None,
    *, store: str | None = None,
    game_id: str | None = None,
) -> None:

    """Raise ``LowDiskSpaceError`` if the path doesn't have enough free space.

    The threshold starts at ``disk_space.min_free_bytes`` and,
    when both ``store`` and ``game_id`` are provided, is
    refined upward using the EWMA-cached save size multiplied
    by ``disk_space.size_multiplier`` (default 1.5×).

    Args:
        path: Target path (or any subpath of an existing mount).
        config: ConfigManager.
        store: Store identifier (for size cache lookup).
        game_id: Per-store game identifier (for size cache lookup).

    Raises:
        LowDiskSpaceError: free space < threshold.
    """
    required = get_min_free_bytes(config)
    if store and game_id:
        try:
            from .save_size_cache import get_observed_size
            cached = get_observed_size(config, store, game_id)
            if cached is not None and not cached.stale:
                multiplier = _get_multiplier(config)
                refined = int(cached.size_bytes * multiplier)
                required = max(required, refined)
        except ImportError:
            pass
    check = check_disk_space(path, required)
    if not check.has_space:
        raise LowDiskSpaceError(
            f"low disk space at {check.path}: "
            f"have {check.free_bytes} bytes, need {required}",
            free_bytes=check.free_bytes,
            required_bytes=required,
        )
def _get_multiplier(config: ConfigManager | None) -> float:
    """Resolve the size-multiplier applied to the cached save size.

    Reads ``disk_space.size_multiplier``. Default 1.5.

    Args:
        config: ConfigManager.

    Returns:
        Multiplier as a float.
    """
    if config is None or not hasattr(config, "get_float"):
        return 1.5
    return cast("float", config.get_float("disk_space.size_multiplier", 1.5))