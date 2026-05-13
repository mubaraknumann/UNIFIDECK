"""Edge presence detection — Flatpak preferred, native fallback.

OP-15c1 | py_modules/unifideck/auth/edge_browser/detection.py

Detection priority:

1. **Flatpak** (``com.microsoft.Edge``) — preferred
   because it's the standard install path on SteamOS
   and is sandboxed;
2. **Native binaries** (``microsoft-edge`` /
   ``microsoft-edge-stable``) — works on regular
   distros.

Two probe levels:

* ``find_edge_cmd`` returns the argv prefix
  (``["flatpak", "run", "com.microsoft.Edge"]`` or
  ``["microsoft-edge"]``) so callers can append per-
  launch args.
* ``is_edge_installed`` is the boolean wrapper.

All ``subprocess`` calls receive a sanitised env via
the ``clean_env_fn`` callback (avoids leaking the
plugin's env vars into the child).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_FLATPAK_APPS = ("com.microsoft.Edge",)
_NATIVE_BINS = ("microsoft-edge", "microsoft-edge-stable")


def flatpak_remote_names(
    clean_env_fn: Callable[[], dict],
    scope: str,
) -> set[str]:
    """Return the names of every configured Flatpak remote at the given scope.

    Used by the installer to know whether
    ``flathub`` is already enabled. Three guards:

    * Invalid scope (not ``--user`` or ``--system``)
      → empty set;
    * Subprocess exception or non-zero exit →
      empty set;
    * Header row (``"name"``) is dropped on parse.

    Args:
        clean_env_fn: callable returning sanitised
            env dict for the subprocess.
        scope: ``"--user"`` or ``"--system"``.

    Returns:
        Set of remote names (lowercase preserved
        from the output).
    """
    if scope not in ("--user", "--system"):
        return set()
    try:
        result = subprocess.run(
            ["flatpak", "remotes", scope, "--columns=name"],
            capture_output=True,
            text=True,
            timeout=5,
            env=clean_env_fn(),
            check=False,
        )
    except Exception:
        return set()
    if result.returncode != 0:
        return set()
    remotes: set[str] = set()
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.lower() == "name":
            continue
        remotes.add(line)
    return remotes


def find_edge_cmd(
    clean_env_fn: Callable[[], dict],
) -> list[str] | None:
    """Return the argv prefix to launch Edge, or ``None`` if not installed.

    Two-stage probe:

    1. If ``flatpak`` is on PATH, try each known
       Flatpak app id via ``_try_flatpak_app``;
    2. Otherwise (or no Flatpak hit) try native
       binaries from PATH.

    Returns ``None`` only when both probes fail —
    the caller's signal to run the installer.

    Args:
        clean_env_fn: env-builder callback.

    Returns:
        Argv prefix list (e.g.
        ``["flatpak", "run", "com.microsoft.Edge"]``),
        or ``None``.
    """
    if shutil.which("flatpak"):
        for app_id in _FLATPAK_APPS:
            cmd = _try_flatpak_app(app_id, clean_env_fn)
            if cmd is not None:
                return cmd
    for binary in _NATIVE_BINS:
        if shutil.which(binary):
            return [binary]
    return None


def _try_flatpak_app(
    app_id: str,
    clean_env_fn: Callable[[], dict],
) -> list[str] | None:
    """Probe ``flatpak info <flag> <app_id>`` at user + system scope.

    Returns the matching ``flatpak run`` argv on the
    first scope that knows the app. Any subprocess
    exception is swallowed (probe is best-effort).

    Args:
        app_id: e.g. ``"com.microsoft.Edge"``.
        clean_env_fn: env-builder callback.

    Returns:
        Argv prefix or ``None``.
    """
    try:
        for flag in ("--user", "--system"):
            result = subprocess.run(
                ["flatpak", "info", flag, app_id],
                capture_output=True,
                timeout=5,
                env=clean_env_fn(),
            )
            if result.returncode == 0:
                return ["flatpak", "run", app_id]
    except Exception:
        pass
    return None


def is_edge_installed(clean_env_fn: Callable[[], dict]) -> bool:
    """Boolean wrapper around ``find_edge_cmd``.

    Convenience for callers that don't need the
    argv prefix.

    Args:
        clean_env_fn: env-builder callback.

    Returns:
        True if any Edge form was detected.
    """
    return find_edge_cmd(clean_env_fn) is not None
