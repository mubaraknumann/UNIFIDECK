"""EWMA-smoothed cache of per-game cloud-save sizes used for download progress estimation."""

from __future__ import annotations
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
if TYPE_CHECKING:
    from ...config import ConfigManager
logger = logging.getLogger(__name__)
_EWMA_ALPHA = 0.3
@dataclass(frozen=True)
class CachedSize:
    """One per-game cached save-folder size estimate.

    Attributes:
        size_bytes: EWMA-smoothed estimate of the save folder size.
        observed_at: Unix timestamp of the last observation.
        sample_count: Number of observations folded into the EWMA.
        stale: True iff the observation is older than the configured TTL.
    """
    size_bytes: int
    observed_at: float
    sample_count: int
    stale: bool
def _resolve_cache_path(config: ConfigManager | None) -> str:
    """Resolve the cache file path from config (with ~ expansion).

    Reads ``disk_space.size_cache_path``. Default
    ``~/.cache/unifideck/cloud_save_sizes.json``.

    Args:
        config: ConfigManager, or ``None`` (uses default).

    Returns:
        Absolute path string.
    """
    if config is None or not hasattr(config, "get_str"):
        raw = "~/.cache/unifideck/cloud_save_sizes.json"
    else:
        raw = config.get_str(
            "disk_space.size_cache_path",
            "~/.cache/unifideck/cloud_save_sizes.json",
        )
    return os.path.expanduser(raw)
def _resolve_ttl_seconds(config: ConfigManager | None) -> int:
    """Resolve the cache-entry TTL from config.

    Reads ``disk_space.size_cache_ttl_seconds``. Default 30 days.

    Args:
        config: ConfigManager, or ``None`` (uses default).

    Returns:
        TTL in seconds.
    """
    if config is None or not hasattr(config, "get_int"):
        return 30 * 24 * 3600
    return config.get_int(
        "disk_space.size_cache_ttl_seconds", 30 * 24 * 3600,
    )
def _load(path: str) -> dict[str, Any]:
    """Load the cache file as a JSON dict.

    Args:
        path: Cache file path.

    Returns:
        Parsed dict, or empty dict on missing file / read error /
        JSON decode error.
    """
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return cast("dict[str, Any]", json.load(fh))
    except (OSError, json.JSONDecodeError) as err:
        logger.warning(
            "[save_size_cache] load failed for %s: %s — "
            "starting empty", path, err,
        )
        return {}
def _save(path: str, data: dict[str, Any]) -> None:
    """Atomically save the cache (tmp file + ``os.replace``).

    Creates the parent directory if needed. Failures are logged
    but not raised — the cache is best-effort.

    Args:
        path: Destination cache file path.
        data: Dict to serialize.
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp_path, path)
    except OSError as err:
        logger.warning("[save_size_cache] save failed for %s: %s", path, err)

def get_observed_size(
    config: ConfigManager | None, store: str, game_id: str,
) -> CachedSize | None:

    """Read the cached save size for one ``store:game_id``.

    Args:
        config: ConfigManager (for path + TTL).
        store: Store identifier.
        game_id: Per-store game identifier.

    Returns:
        A ``CachedSize`` (with ``stale`` flagged when older than
        the TTL), or ``None`` if no observation has been recorded.
    """
    path = _resolve_cache_path(config)
    data = _load(path)
    key = f"{store}:{game_id}"
    entry = data.get(key)
    if not entry:
        return None
    ttl = _resolve_ttl_seconds(config)
    age = time.time() - entry.get("observed_at", 0)
    return CachedSize(
        size_bytes=int(entry.get("size_bytes", 0)),
        observed_at=float(entry.get("observed_at", 0)),
        sample_count=int(entry.get("sample_count", 0)),
        stale=(age > ttl),
    )
def record_observed_size(
    config: ConfigManager | None, store: str, game_id: str, size_bytes: int,
) -> None:
    """Record a new save-size observation, folded into the EWMA.

    First observation seeds the EWMA. Subsequent observations
    use alpha=0.3. Negative sizes are rejected with a warning.

    Args:
        config: ConfigManager.
        store: Store identifier.
        game_id: Per-store game identifier.
        size_bytes: Observed size in bytes.
    """
    if size_bytes < 0:
        logger.warning(
            "[save_size_cache] negative size for %s:%s ignored",
            store, game_id,
        )
        return
    path = _resolve_cache_path(config)
    data = _load(path)
    key = f"{store}:{game_id}"
    existing = data.get(key)
    if existing is None or existing.get("sample_count", 0) == 0:
        new_size = size_bytes
        new_count = 1
    else:
        old = existing["size_bytes"]
        new_size = int(_EWMA_ALPHA * size_bytes + (1 - _EWMA_ALPHA) * old)
        new_count = existing["sample_count"] + 1
    data[key] = {
        "size_bytes": new_size,
        "observed_at": time.time(),
        "sample_count": new_count,
    }
    _save(path, data)
def measure_directory_size(directory: str) -> int:
    """Walk a directory and return its total file-byte count.

    Individual stat failures are skipped; a top-level walk
    failure logs a warning and returns 0.

    Args:
        directory: Path to measure.

    Returns:
        Total size in bytes (0 if the directory doesn't exist
        or can't be walked).
    """
    if not os.path.isdir(directory):
        return 0
    total = 0
    try:
        for root, _, files in os.walk(directory):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
    except OSError as err:
        logger.warning(
            "[save_size_cache] walk failed for %s: %s", directory, err,
        )
        return 0
    return total