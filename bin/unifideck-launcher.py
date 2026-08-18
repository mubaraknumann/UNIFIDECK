#!/usr/bin/env python3
"""Steam shortcut launcher entry point — legacy CLI wrapper.

This thin wrapper is what Steam invokes when a user clicks a
non-Steam shortcut created by Unifideck. It runs in Steam's own
process context, where ``LD_LIBRARY_PATH`` and ``LD_PRELOAD`` are
populated by the Steam Runtime — values that systematically break
non-Steam binaries (Wine, Proton, store CLIs). Both variables are
scrubbed before any Python import so the dispatcher inherits a
clean environment.

The wrapper has two responsibilities :

    1. Sanitise the inherited environment.
    2. Prepend the plugin's ``py_modules`` directory to ``sys.path``
       so ``unifideck.launcher.dispatcher`` resolves at import time.

All real work is delegated to ``dispatcher_main(argv)``.
"""

from __future__ import annotations

import os

# Steam Runtime variables must be scrubbed before any other module
# is imported, including ``sys`` and ``pathlib`` — the dispatcher
# spawns subprocesses (Wine, Proton, store CLIs) that fail
# unpredictably when these variables leak into their environment.
os.environ.pop("LD_LIBRARY_PATH", None)
os.environ.pop("LD_PRELOAD", None)

import sys
from pathlib import Path

# See bin/unifideck-launcher's ``_escape_pressure_vessel``/
# ``_reexec_on_modern_python`` for the full rationale — kept in sync
# manually since this file is not the one build-plugin.sh actually ships
# (see its file checklist), but is kept functionally identical to avoid
# drift if it's ever used as a source.
_MODERN_PYTHON_CANDIDATES: tuple[str, ...] = (
    "/usr/bin/python3.14",
    "/usr/bin/python3.13",
    "/usr/bin/python3.12",
    "/usr/bin/python3.11",
    "/usr/bin/python3.10",
)
_REEXEC_GUARD_ENV = "_UNIFIDECK_LAUNCHER_REEXECED"
_ESCAPE_CLIENT = "steam-runtime-launch-client"

def _in_pressure_vessel() -> bool:
    """Whether this process is running inside a pressure-vessel container."""
    return (
        os.environ.get("container") == "pressure-vessel"  # noqa: SIM112
        or os.path.isdir("/run/pressure-vessel")
    )

def _find_on_path(name: str) -> str | None:
    """Minimal ``shutil.which`` substitute using only ``os``."""
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, name) if directory else name
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None

def _escape_pressure_vessel() -> None:
    """Re-exec OUTSIDE Steam's pressure-vessel container, if inside one.

    See ``bin/unifideck-launcher``'s twin function for the full rationale:
    the container's own ``python3`` is too old for this codebase AND the
    container has no newer interpreter to fall back to on its own
    filesystem, so the only real fix is escaping via
    ``steam-runtime-launch-client --alongside-steam`` (same tool
    ``launcher.proton.infrastructure.container_escape`` uses for umu).
    """
    if not _in_pressure_vessel():
        return
    if os.environ.get(_REEXEC_GUARD_ENV):
        return
    client = _find_on_path(_ESCAPE_CLIENT)
    if client is None:
        return
    env = dict(os.environ)
    env[_REEXEC_GUARD_ENV] = "1"
    script_path = os.path.abspath(__file__)
    argv = [
        client, "--alongside-steam", "--",
        "python3", script_path, *sys.argv[1:],
    ]
    try:
        os.execve(client, argv, env)
    except OSError:
        return

def _reexec_on_modern_python() -> None:
    """Re-exec onto a modern (>=3.10) Python if the current one is too old.

    Second line of defence for the (unobserved) case of running under an
    old Python OUTSIDE a pressure-vessel container.
    """
    if sys.version_info >= (3, 10):
        return
    if os.environ.get(_REEXEC_GUARD_ENV):
        return
    for candidate in _MODERN_PYTHON_CANDIDATES:
        if not os.path.isfile(candidate):
            continue
        env = dict(os.environ)
        env[_REEXEC_GUARD_ENV] = "1"
        script_path = os.path.abspath(__file__)
        try:
            os.execve(candidate, [candidate, script_path, *sys.argv[1:]], env)
        except OSError:
            continue

_escape_pressure_vessel()
_reexec_on_modern_python()

def _bootstrap_path() -> None:
    """Make the plugin's ``py_modules`` directory importable.

    Resolves the plugin root relative to this script's location
    (``<plugin>/bin/unifideck-launcher``) and inserts
    ``<plugin>/py_modules`` at the front of ``sys.path`` so the
    ``unifideck`` package imports cleanly regardless of the
    caller's working directory.

    The function is a no-op when ``py_modules/`` is missing — that
    case is reported later by ``main()`` with a clearer error
    message than ``ImportError: No module named 'unifideck'``.
    """
    plugin_dir = Path(__file__).resolve().parent.parent
    py_modules = plugin_dir / "py_modules"
    if py_modules.is_dir():
        sys.path.insert(0, str(py_modules))

def main() -> int:
    """Entry point — bootstrap, hand off to the dispatcher, return its code.

    Returns ``2`` if ``unifideck.launcher.dispatcher`` cannot be
    imported (broken install, missing ``py_modules`` directory,
    syntax error in the dispatcher itself). Otherwise returns
    whatever ``dispatcher_main`` returns ; Steam surfaces a
    non-zero exit code as a "game failed to launch" toast.

    The two stderr lines emitted on import failure carry the
    exception message and the resolved plugin directory — together
    they let the user diagnose a broken install without attaching
    a debugger to the Steam process.
    """
    _bootstrap_path()
    try:
        from unifideck.launcher.dispatcher import main as dispatcher_main
    except ImportError as exc:
        print(
            f"[unifideck-launcher] failed to import dispatcher: {exc}",
            file=sys.stderr,
        )
        print(
            f"[unifideck-launcher] plugin_dir="
            f"{Path(__file__).resolve().parent.parent}",
            file=sys.stderr,
        )
        return 2
    return dispatcher_main(sys.argv)

if __name__ == "__main__":
    sys.exit(main())
