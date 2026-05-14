"""Wine prefix path normalization — distinguishes the prefix root from the inner ``pfx/`` directory."""

from __future__ import annotations
import logging
from pathlib import Path
logger = logging.getLogger(__name__)
PathLike = str | Path
def normalize_prefix_root(prefix_path: PathLike) -> Path:
    """Strip trailing ``pfx`` segments to return the canonical prefix root.

    Args:
        prefix_path: Any path inside (or equal to) the Wine prefix.

    Returns:
        The resolved prefix root (the directory CONTAINING
        ``pfx``, if any).
    """
    p = Path(prefix_path).resolve() if isinstance(prefix_path, str) \
        else prefix_path.resolve()
    while p.name == "pfx":
        p = p.parent
    return p
def resolve_registry_prefix(prefix_root: PathLike) -> Path:
    """Pick the directory containing the Wine registry files.

    Modern prefixes have ``user.reg`` at the root. Older /
    Proton prefixes have ``pfx/user.reg``. Falls back to
    ``pfx`` if it exists, else the root.

    Args:
        prefix_root: Canonical prefix root.

    Returns:
        Path to use for registry I/O.
    """
    root = Path(prefix_root) if isinstance(prefix_root, str) \
        else prefix_root
    direct = root / "user.reg"
    pfx = root / "pfx"
    pfx_reg = pfx / "user.reg"
    if direct.exists():
        return root
    if pfx_reg.exists():
        return pfx
    if pfx.is_dir():
        return pfx
    return root
def resolve_drive_c(prefix_root: PathLike) -> Path | None:
    """Pick the ``drive_c`` directory for a prefix.

    Tries ``pfx/drive_c`` first, then ``drive_c`` directly.

    Args:
        prefix_root: Canonical prefix root.

    Returns:
        Path to ``drive_c``, or ``None`` if neither layout exists.
    """
    root = Path(prefix_root) if isinstance(prefix_root, str) \
        else prefix_root
    modern = root / "pfx" / "drive_c"
    if modern.is_dir():
        return modern
    legacy = root / "drive_c"
    if legacy.is_dir():
        return legacy
    return None