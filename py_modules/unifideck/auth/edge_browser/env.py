"""Sanitised env builder for Edge launches under SteamOS / Gamescope.

OP-15c2 | py_modules/unifideck/auth/edge_browser/env.py

Building the env for a subprocess that needs to talk
to the user's display server is hard on SteamOS:

* The plugin itself runs in Decky's sandbox, missing
  most session vars;
* Gamescope re-parents the display, so even basic
  ``DISPLAY``/``WAYLAND_DISPLAY`` discovery is
  non-trivial;
* Steam injects its own ``LD_PRELOAD`` /
  ``LD_LIBRARY_PATH`` that breaks Edge.

The strategy is a four-source merge into one result
dict:

1. Seed from the plugin's own env (covers cases
   where Decky actually has the vars);
2. Read ``/run/user/<uid>/gamescope-environment``
   (Gamescope writes session vars here);
3. Scan running ``steam`` / ``gamescope-session`` /
   ``gamescope`` processes via
   ``/proc/<pid>/environ``;
4. Apply fallbacks for anything still missing
   (``DISPLAY=:0``, ``XDG_RUNTIME_DIR`` default,
   D-Bus socket path, Xauthority files).

``clean_env`` then strips Steam-injected dangerous
vars (``LD_*``) and sets some explicit defaults
(``GTK_MODULES=""`` disables Steam's input-method
hook which crashes Edge).
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_SESSION_ENV_KEYS = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "GAMESCOPE_WAYLAND_DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
    "DESKTOP_SESSION",
    "GTK_IM_MODULE",
    "QT_IM_MODULE",
    "XAUTHORITY",
    "XDG_RUNTIME_DIR",
    "XMODIFIERS",
    "XDG_SESSION_TYPE",
    "XDG_CURRENT_DESKTOP",
)


def _seed_from_own_env(result: dict[str, str]) -> None:
    """Copy ``_SESSION_ENV_KEYS`` from this process's env into ``result``.

    First pass of the four-source merge. Skips
    falsy values (empty string = "not set").

    Args:
        result: dict to populate (mutated).
    """
    for key in _SESSION_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            result[key] = value


def _read_gamescope_env_file(
    runtime_dir: str,
    result: dict[str, str],
) -> None:
    """Parse ``$XDG_RUNTIME_DIR/gamescope-environment`` if present.

    Gamescope writes session env vars to this file
    in ``KEY=VALUE`` format. The parser:

    * Skips empty/no-``=`` lines;
    * Filters to ``_SESSION_ENV_KEYS``;
    * Honours "first source wins" (doesn't
      overwrite values already set).

    File read errors are swallowed silently —
    absence is expected when Gamescope isn't
    running.

    Args:
        runtime_dir: ``/run/user/<uid>`` path.
        result: dict to populate (mutated).
    """
    gamescope_env = Path(runtime_dir) / "gamescope-environment"
    if not gamescope_env.exists():
        return
    try:
        with gamescope_env.open(
            encoding="utf-8",
            errors="replace",
        ) as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key in _SESSION_ENV_KEYS and key not in result and value:
                    result[key] = value
    except OSError:
        pass


def _parse_proc_environ(
    pid: str,
    result: dict[str, str],
) -> bool:
    """Read ``/proc/<pid>/environ`` and merge missing keys; report DISPLAY success.

    ``environ`` is a NUL-separated key=value list of
    the process's environ block. The reader:

    * Reads the whole file as bytes;
    * Splits on ``\\x00``;
    * Decodes each entry as UTF-8 (errors=replace);
    * Merges into result honouring "first wins".

    Returns True when DISPLAY or WAYLAND_DISPLAY is
    populated — signals the caller's loop that the
    important vars were found (can stop scanning).

    PermissionError is expected for processes owned
    by other users (we filter by uid, but still
    defensive).

    Args:
        pid: target process pid as string.
        result: dict to populate (mutated).

    Returns:
        True if DISPLAY/WAYLAND_DISPLAY now set.
    """
    try:
        with Path(f"/proc/{pid}/environ").open("rb") as f:
            env_bytes = f.read()
    except (PermissionError, FileNotFoundError, OSError):
        return False
    for entry in env_bytes.split(b"\x00"):
        decoded = entry.decode("utf-8", errors="replace")
        if "=" not in decoded:
            continue
        key, value = decoded.split("=", 1)
        if key in _SESSION_ENV_KEYS and key not in result and value:
            result[key] = value
    return bool(
        result.get("DISPLAY") or result.get("WAYLAND_DISPLAY"),
    )


def _scan_steam_process_env(
    uid: int,
    result: dict[str, str],
) -> None:
    """``pgrep`` for Steam-family processes and harvest their env vars.

    Walks three process names in priority order:

    * ``steam`` — the main process, has the user
      session env;
    * ``gamescope-session`` — wrapping shell;
    * ``gamescope`` — the compositor itself.

    For each match, calls ``_parse_proc_environ``.
    Stops scanning on the first one that yields
    DISPLAY/WAYLAND_DISPLAY (the typical case —
    once we have those, the rest of the env
    follows).

    All errors swallowed at DEBUG — env detection
    is best-effort and fallbacks handle the no-match
    case.

    Args:
        uid: user id for pgrep filter.
        result: dict to populate (mutated).
    """
    try:
        for proc_name in (
            "steam",
            "gamescope-session",
            "gamescope",
        ):
            pids = (
                subprocess.run(
                    ["pgrep", "-u", str(uid), "-x", proc_name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                .stdout.strip()
                .split("\n")
            )
            for raw_pid in pids:
                pid = raw_pid.strip()
                if not pid:
                    continue
                if _parse_proc_environ(pid, result):
                    logger.info(
                        "[Edge] Session env detected from "
                        "PID %s (%s): DISPLAY=%s "
                        "WAYLAND_DISPLAY=%s",
                        pid,
                        proc_name,
                        result.get("DISPLAY"),
                        result.get("WAYLAND_DISPLAY"),
                    )
                    return
    except Exception as e:
        logger.debug(
            "[Edge] Session env detection error: %s",
            e,
        )


def _apply_fallbacks(
    uid: int,
    home: str,
    runtime_dir: str,
    result: dict[str, str],
) -> None:
    """Patch missing keys with conventional defaults.

    Six fallbacks applied in turn:

    * DISPLAY → ``:0`` if no display server var at
      all;
    * XDG_RUNTIME_DIR → ``/run/user/<uid>``;
    * DBUS_SESSION_BUS_ADDRESS →
      ``unix:path=<runtime>/bus`` if that file
      exists;
    * XAUTHORITY → first ``xauth_*`` file in
      runtime_dir, or ``~/.Xauthority`` if present;
    * WAYLAND_DISPLAY → fall back to
      ``GAMESCOPE_WAYLAND_DISPLAY`` if the socket
      exists (some Gamescope versions only set the
      gamescope-specific var);
    * XMODIFIERS → ``@im=Steam`` when Steam's GTK
      IM module is in use (Edge needs both to
      handle input correctly).

    Args:
        uid: user id (kept for future fallbacks).
        home: user home directory.
        runtime_dir: ``/run/user/<uid>``.
        result: dict to populate (mutated).
    """
    if not result.get("DISPLAY") and not result.get("WAYLAND_DISPLAY"):
        result["DISPLAY"] = ":0"
    if not result.get("XDG_RUNTIME_DIR"):
        result["XDG_RUNTIME_DIR"] = runtime_dir
    if "DBUS_SESSION_BUS_ADDRESS" not in result and Path(f"{runtime_dir}/bus").exists():
        result["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime_dir}/bus"
    if "XAUTHORITY" not in result:
        xauth_files = [str(p) for p in Path(runtime_dir).glob("xauth_*")]
        if xauth_files:
            result["XAUTHORITY"] = xauth_files[0]
        elif (Path(home) / ".Xauthority").exists():
            result["XAUTHORITY"] = str(Path(home) / ".Xauthority")
    if (
        not result.get("WAYLAND_DISPLAY")
        and result.get("GAMESCOPE_WAYLAND_DISPLAY")
        and result.get("XDG_RUNTIME_DIR")
    ):
        gamescope_socket = (
            Path(result["XDG_RUNTIME_DIR"]) / result["GAMESCOPE_WAYLAND_DISPLAY"]
        )
        if gamescope_socket.exists():
            result["WAYLAND_DISPLAY"] = result["GAMESCOPE_WAYLAND_DISPLAY"]
    if result.get("GTK_IM_MODULE") == "Steam" and not result.get("XMODIFIERS"):
        result["XMODIFIERS"] = "@im=Steam"


def _detect_session_env(uid: int, home: str) -> dict[str, str]:
    """Run the full four-source detection pipeline and return the result.

    Args:
        uid: user id.
        home: user home directory.

    Returns:
        Populated env dict ready to merge into
        the subprocess env.
    """
    result: dict[str, str] = {}
    runtime_dir = f"/run/user/{uid}"
    _seed_from_own_env(result)
    _read_gamescope_env_file(runtime_dir, result)
    _scan_steam_process_env(uid, result)
    _apply_fallbacks(uid, home, runtime_dir, result)
    return result


def clean_env() -> dict:
    """Build a clean env dict suitable for spawning Edge.

    Three layers:

    1. Copy the plugin's own env minus
       ``LD_LIBRARY_PATH``/``LD_PRELOAD`` (which
       Steam injects with Steam-specific paths
       that break non-Steam binaries);
    2. Layer the detected session env on top;
    3. Set defaults for ``XDG_RUNTIME_DIR``,
       Steam app IDs (zero values silence
       Steam-detection logic in some flatpaks),
       and explicitly empty ``GTK_MODULES``
       (drops Steam's input-method hook which
       crashes Edge on launch).

    Returns:
        Subprocess-ready env dict.
    """
    home = str(Path.home())
    uid = Path(home).stat().st_uid
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("LD_LIBRARY_PATH", "LD_PRELOAD")
    }
    env.update(_detect_session_env(uid, home))
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("SteamGameId", "0")
    env.setdefault("STEAM_COMPAT_APP_ID", "0")
    env.setdefault("SteamAppId", "0")
    env["GTK_MODULES"] = ""
    return env
