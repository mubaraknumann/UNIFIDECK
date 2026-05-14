"""Strip Epic-specific authentication args from the command line before passing them to UMU (Epic does not accept them)."""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ...types.context import LaunchContext
logger = logging.getLogger(__name__)
_STRIP_PREFIXES: tuple[str, ...] = (
    "-AUTH_TYPE=",
    "-AUTH_PASSWORD=",
)
def strip_epic_auth_args(args: list[str]) -> tuple[list[str], list[str]]:
    """Filter Epic auth args (``-AUTH_TYPE=``, ``-AUTH_PASSWORD=``) out of an argv list.

    Ubisoft games launched through the Epic-wrapper fallback
    would otherwise pass these Epic-only flags to UPC, which
    rejects them.

    Args:
        args: argv list to filter.

    Returns:
        Tuple ``(filtered_args, stripped_args)``. Both keep
        original ordering. Stripped values are logged with
        secrets redacted.
    """
    filtered: list[str] = []
    stripped: list[str] = []
    for arg in args:
        if any(arg.startswith(p) for p in _STRIP_PREFIXES):
            stripped.append(arg)
            continue
        filtered.append(arg)
    if stripped:
        logger.info(
            "[auth_args_stripper] stripped %d Epic auth args: %s",
            len(stripped),
            [s.split("=", 1)[0] + "=<redacted>" for s in stripped],
        )
    return filtered, stripped
def should_strip_for_launch_context(ctx: LaunchContext) -> bool:
    """Decide whether the auth-args stripper applies to a given launch.

    Currently only Ubisoft titles whose exe path contains
    ``UplayLaunch`` need the strip.

    Args:
        ctx: Launch context.

    Returns:
        True iff stripping applies.
    """
    try:
        store = getattr(ctx, "store", "")
        exe_path = str(getattr(ctx, "exe_path", ""))
        return (
            store == "ubisoft" and "UplayLaunch" in exe_path
        )
    except Exception:
        return False