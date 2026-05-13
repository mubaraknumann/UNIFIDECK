"""Defensive config-reading helpers — survive missing managers.

OP-21a | py_modules/unifideck/utils/config_helpers.py

Most of the codebase reads configuration through a
``ConfigManager`` instance, but a handful of edge cases
need to work without one:

* cold-start paths that run before the manager exists;
* dev / test contexts that construct objects without
  the full plugin scaffolding;
* fallback paths that should degrade gracefully when
  the manager wasn't injected.

``get_cfg`` is the standard read; ``read_config_int_cold_start``
parses the config JSON directly when even ``ConfigManager``
isn't available.

The module also tracks call sites that hit the ``None``
branch and logs each unique site exactly once (a strong
signal that a service forgot to receive the config
injection).
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from unifideck.config import ConfigManager

logger = logging.getLogger(__name__)

_none_sites_seen: set[tuple[str, int]] = set()


def get_cfg(
    config: ConfigManager | None,
    key: str,
    default: Any,
) -> Any:
    """Read ``key`` from ``config``, falling back to ``default`` on any failure.

    Two arms:

    * ``config is None`` — log the call site (module +
      lineno) once at WARN and return ``default``. The
      one-shot log keeps plugin logs clean even when a
      missing manager is hit in a hot loop.
    * ``config`` present but ``get`` raises — silently
      fall back to ``default`` (the manager itself logs
      its own errors).

    Args:
        config: optional config manager.
        key: dotted config key.
        default: fallback value.

    Returns:
        Config value or ``default``.
    """
    if config is None:
        frame = sys._getframe(1)
        site = (frame.f_globals.get("__name__", "?"), frame.f_lineno)
        if site not in _none_sites_seen:
            _none_sites_seen.add(site)
            logger.warning(
                "[config_helpers] config=None at %s:%d key=%r "
                "— falling back to default=%r. Likely a forgotten "
                "ConfigManager injection.",
                site[0],
                site[1],
                key,
                default,
            )
        return default
    try:
        return config.get(key, default)
    except Exception:
        return default


_COLD_START_CONFIG_PATH = "~/.local/share/unifideck/config.json"


def read_config_int_cold_start(key: str, default: int) -> int:
    """Parse the user config JSON directly and return ``key`` as an int.

    Used by code that runs **before** the
    ``ConfigManager`` exists (e.g. the very early
    bootstrap pre-Layer-3). Reads the user config file
    at the conventional path, walks the dotted key
    path, returns the value if it's a positive int.

    Defensive return-``default`` on every failure mode:

    * file missing → default;
    * unreadable / malformed JSON → default;
    * key not found → default;
    * value isn't a positive int → default.

    Args:
        key: dotted key path.
        default: fallback positive int.

    Returns:
        Resolved value or ``default``.
    """
    import json
    from pathlib import Path as _P

    config_path = _P(_COLD_START_CONFIG_PATH).expanduser()
    if not config_path.is_file():
        return default
    try:
        with config_path.open() as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default
    node = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    if not isinstance(node, int) or node <= 0:
        return default
    return node
