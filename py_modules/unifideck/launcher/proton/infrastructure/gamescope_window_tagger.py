"""Tag game windows with STEAM_GAME so gamescope brings them to the foreground.

umu's own ``monitor_windows`` does this, but only when ``is_steammode`` is
True — which requires ``container=flatpak``.  We run inside
``container=pressure-vessel`` so umu skips that path.

The fix: game windows (WM_CLASS = "steam_app_<appid>") appear on display ``:0``
(the gamescope compositor), not ``:1``.  Setting STEAM_GAME=<appid> on them
causes gamescope to add the app to FOCUSABLE_APPS and switch focus immediately.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DISPLAY = ":0"
_POLL_INTERVAL = 0.3
_MAX_RUNTIME = 300


def _find_umu_zipapp() -> Path | None:
    """Return path to the umu zipapp bundled with this plugin."""
    # infrastructure/ → proton/ → launcher/ → unifideck/ → py_modules/ → plugin_root/
    here = Path(__file__).resolve().parent
    plugin_root = here.parents[4]
    candidate = plugin_root / "bin" / "umu" / "umu" / "umu_run.py"
    if candidate.is_file():
        return candidate
    return None


def _tag_windows(appid: int, stop_event: threading.Event) -> None:
    umu_zip = _find_umu_zipapp()
    if umu_zip is None:
        logger.warning("[gamescope_tagger] umu zipapp not found, cannot tag")
        return

    zip_str = str(umu_zip)
    if zip_str not in sys.path:
        sys.path.insert(0, zip_str)

    try:
        from Xlib import X
        from Xlib.display import Display
    except ImportError as e:
        logger.warning("[gamescope_tagger] Xlib import failed: %s", e)
        return

    try:
        d = Display(_DISPLAY)
    except Exception as e:
        logger.warning("[gamescope_tagger] cannot open display %s: %s", _DISPLAY, e)
        return

    root = d.screen().root
    # Listen for new windows on :0 (gamescope compositor)
    root.change_attributes(event_mask=X.SubstructureNotifyMask)
    d.flush()

    atom_steam_game = d.intern_atom("STEAM_GAME", only_if_exists=False)
    wm_class_str = f"steam_app_{appid}"
    tagged: set[int] = set()
    deadline = time.monotonic() + _MAX_RUNTIME
    logger.info("[gamescope_tagger] watching :0 for WM_CLASS=%s", wm_class_str)

    # Also tag any windows that already exist on :0 before we started listening
    try:
        for child in root.query_tree().children:
            _try_tag(d, child, atom_steam_game, wm_class_str, appid, tagged)
    except Exception as e:
        logger.debug("[gamescope_tagger] initial scan error: %s", e)

    while not stop_event.is_set() and time.monotonic() < deadline:
        while d.pending_events():
            ev = d.next_event()
            if ev.type != X.CreateNotify:
                continue
            try:
                _try_tag(d, ev.window, atom_steam_game, wm_class_str, appid, tagged)
            except Exception as e:
                logger.debug("[gamescope_tagger] event error: %s", e)
        time.sleep(_POLL_INTERVAL)

    d.close()
    logger.info("[gamescope_tagger] done, tagged %d window(s)", len(tagged))


def _try_tag(d, window, atom_steam_game, wm_class_str: str, appid: int, tagged: set) -> None:
    wid = window.id
    if wid in tagged:
        return
    cls = window.get_wm_class()
    if not cls:
        return
    # WM_CLASS is a tuple (instance, class); game windows have both set to steam_app_<appid>
    if wm_class_str not in cls:
        return
    window.change_property(atom_steam_game, d.get_atom("CARDINAL"), 32, [appid])
    d.flush()
    tagged.add(wid)
    logger.info("[gamescope_tagger] STEAM_GAME=%d set on wid=0x%x", appid, wid)


def start_window_tagger(appid: int) -> threading.Event:
    """Start a daemon thread that tags game windows on :0 with STEAM_GAME=appid."""
    stop = threading.Event()
    t = threading.Thread(
        target=_tag_windows,
        args=(appid, stop),
        daemon=True,
        name=f"gamescope-tagger-{appid}",
    )
    t.start()
    logger.info("[gamescope_tagger] started for appid=%d", appid)
    return stop
