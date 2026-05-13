"""Edge launch flows for auth (windowed) and xCloud (kiosk) modes.

OP-15c5 | py_modules/unifideck/auth/edge_browser/launch.py

Two launch flows that share the same setup pipeline
but differ in flags:

* **Auth** — windowed, fullscreen, points at the
  OAuth URL via ``--app=`` (chromeless window mode,
  no URL bar);
* **xCloud** — kiosk mode, scaled device pixel ratio
  for the Steam Deck's screen, autoplay enabled.

Both go through ``_prepare_for_launch`` (kill any
prior Edge + cleanup stale singleton + ensure profile
dir exists) then ``_spawn_edge_process`` (build the
clean env + spawn with stderr→log_file + own pgrp for
later signalling).
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from .env import clean_env

if TYPE_CHECKING:
    from .edge import EdgeBrowser

logger = logging.getLogger(__name__)


def _prepare_for_launch(browser: EdgeBrowser) -> list[str] | None:
    """Common setup: kill prior Edge, clean stale state, locate the binary.

    Three-step:

    1. ``browser.kill()`` — kill any leftover from
       prior launch (idempotent if no process);
    2. ``cleanup_stale_profile_state`` — remove
       stale Singleton* files;
    3. ``find_cmd`` — locate the Edge binary
       (Flatpak or native).

    Also creates the profile dir if missing.

    Args:
        browser: ``EdgeBrowser`` instance.

    Returns:
        Argv prefix for the launch, or ``None`` if
        Edge isn't installed.
    """
    browser.kill()
    browser.cleanup_stale_profile_state()
    cmd = browser.find_cmd()
    if not cmd:
        return None
    from .edge import PROFILE_DIR

    Path(PROFILE_DIR).mkdir(parents=True, exist_ok=True)
    return cmd


def _spawn_edge_process(
    browser: EdgeBrowser,
    args: list[str],
    log_mode: str,
    label: str,
) -> bool:
    """Build env, redirect stderr to log file, ``Popen`` with own pgrp.

    Logs the first 7 argv elements at INFO so
    operators see what was launched without flooding
    with the full flag list. Also logs the resolved
    DISPLAY / WAYLAND_DISPLAY / SteamAppId for
    diagnostics.

    ``preexec_fn=os.setpgrp`` puts Edge in its own
    process group so the later kill can signal the
    whole tree (Edge spawns renderer/GPU/network
    helpers as children).

    Stderr file handle is opened before ``Popen``
    but closed in ``finally`` — the subprocess
    inherits the FD via dup, so closing our copy
    after spawn is safe.

    Args:
        browser: ``EdgeBrowser`` instance.
        args: full argv.
        log_mode: ``"w"`` for auth (fresh log),
            ``"a"`` for xCloud (append after auth).
        label: ``"Auth"`` or ``"xCloud"`` (log
            prefix).

    Returns:
        True on successful spawn.
    """
    from .edge import LOG_FILE

    env = clean_env()
    logger.info(
        "[Edge] Launching %s browser: %s...",
        label.lower(),
        " ".join(args[:7]),
    )
    logger.info(
        "[Edge] %s browser env DISPLAY=%s WAYLAND_DISPLAY=%s SteamAppId=%s",
        label,
        env.get("DISPLAY"),
        env.get("WAYLAND_DISPLAY"),
        env.get("SteamAppId"),
    )
    stderr_fh = None
    try:
        stderr_fh = Path(LOG_FILE).open(log_mode)
    except Exception:
        pass
    try:
        browser.process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=(stderr_fh if stderr_fh else subprocess.DEVNULL),
            env=env,
            preexec_fn=os.setpgrp,
        )
        logger.info(
            "[Edge] %s browser PID: %s",
            label,
            browser.process.pid,
        )
        return True
    except Exception as e:
        logger.exception(
            "[Edge] Failed to launch %s browser: %s",
            label.lower(),
            e,
        )
        return False
    finally:
        if stderr_fh is not None:
            stderr_fh.close()


def launch_auth(browser: EdgeBrowser, auth_url: str) -> bool:
    """Launch Edge in windowed fullscreen mode pointing at ``auth_url``.

    Flags:

    * ``--app=<url>`` — chromeless window (no URL
      bar, no tabs);
    * ``--class=unifideck-auth`` — WM class for
      window identification;
    * ``--remote-debugging-port`` — CDP port for the
      monitor;
    * ``--user-data-dir`` — isolated profile;
    * ``--start-fullscreen`` + ``--enable-touch-events``
      + locale → the Steam Deck UX hooks.

    Args:
        browser: ``EdgeBrowser`` instance.
        auth_url: the OAuth start URL.

    Returns:
        True on successful spawn.
    """
    cmd = _prepare_for_launch(browser)
    if not cmd:
        logger.warning("[Edge] No compatible browser found for auth")
        return False
    from .edge import _BASE_FLAGS, PROFILE_DIR

    args = (
        cmd
        + [
            f"--app={auth_url}",
            "--class=unifideck-auth",
            f"--remote-debugging-port={browser.cdp_port}",
            f"--user-data-dir={PROFILE_DIR}",
        ]
        + _BASE_FLAGS
        + [
            "--start-fullscreen",
            "--enable-touch-events",
            "--window-size=1280,800",
            f"--lang={browser.locale_fn().split('-')[0]}",
        ]
    )
    return _spawn_edge_process(browser, args, log_mode="w", label="Auth")


def launch_xcloud(browser: EdgeBrowser, xcloud_url: str) -> bool:
    """Launch Edge in kiosk mode for xCloud streaming.

    Uses CDP port ``cdp_port + 1`` so the auth +
    xCloud flows can coexist if needed. Kiosk-
    specific flags:

    * ``--kiosk`` — fullscreen, no exit shortcut;
    * ``--autoplay-policy=no-user-gesture-required``
      — xCloud needs autoplay;
    * Scale factor 1.25 — matches the Deck screen
      density.

    Log mode ``"a"`` appends to the existing log so
    a full auth → xCloud flow keeps both halves of
    the trace.

    Args:
        browser: ``EdgeBrowser`` instance.
        xcloud_url: deep-link URL into xCloud.

    Returns:
        True on successful spawn.
    """
    cmd = _prepare_for_launch(browser)
    if not cmd:
        logger.warning("[Edge] No compatible browser found for xCloud")
        return False
    from .edge import _BASE_FLAGS, PROFILE_DIR

    xcloud_cdp_port = browser.cdp_port + 1
    args = (
        cmd
        + [
            "--kiosk",
            "--class=unifideck-xcloud",
            f"--remote-debugging-port={xcloud_cdp_port}",
            f"--user-data-dir={PROFILE_DIR}",
        ]
        + _BASE_FLAGS
        + [
            "--autoplay-policy=no-user-gesture-required",
            "--window-size=1024,720",
            "--force-device-scale-factor=1.25",
            "--device-scale-factor=1.25",
            f"--lang={browser.locale_fn().split('-')[0]}",
            xcloud_url,
        ]
    )
    logger.info(
        "[Edge] Launching xCloud kiosk: %s",
        xcloud_url[:80],
    )
    return _spawn_edge_process(browser, args, log_mode="a", label="xCloud")
