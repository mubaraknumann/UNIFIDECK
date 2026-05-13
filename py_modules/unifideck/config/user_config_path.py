"""User config file path resolver — env-var + XDG fallback.

OP-10a | py_modules/unifideck/config/user_config_path.py

Three-step resolution (first match wins):

1. ``$UNIFIDECK_USER_CONFIG`` env var — explicit
   override, expanded with ``~`` resolution;
2. ``$XDG_CONFIG_HOME/unifideck/config.json`` — XDG
   standard;
3. ``~/.config/unifideck/config.json`` — conventional
   fallback.

The file itself may not exist yet (first run) — callers
treat that as "no user overrides".
"""

from __future__ import annotations

import os


def resolve_user_config_path() -> str:
    """Return the resolved path of the user config file (may not exist yet).

    Implements the three-step fallback chain
    described in the module docstring. Always returns
    a string — never ``None`` — even when none of the
    sources exist on disk.

    Returns:
        Absolute path string (file may not exist).
    """
    env = os.environ.get("UNIFIDECK_USER_CONFIG")
    if env:
        return os.path.expanduser(env)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return os.path.join(xdg, "unifideck", "config.json")
    return os.path.expanduser("~/.config/unifideck/config.json")
