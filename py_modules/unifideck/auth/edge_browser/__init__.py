"""Microsoft Edge installer + launcher subpackage.

OP-15c | py_modules/unifideck/auth/edge_browser/__init__.py

Microsoft's xCloud + Xbox login flows only work in
Edge (the OAuth pages refuse other UAs even via
spoofing). This subpackage:

* Detects whether Edge is installed (native binary or
  Flatpak);
* Installs it via Flatpak if missing
  (``installer.py``);
* Launches it with an isolated profile and remote-
  debugging port for CDP control
  (``launch.py``, ``profile.py``);
* Hosts a tiny CDP client tuned for the
  capture-token flow (``cdp_client.py``);
* Exposes ``EdgeBrowser`` — the high-level facade
  (``edge.py``).

``env.py`` and ``process_ops.py`` are shared
utilities (clean environment dict, process spawn/kill
helpers).
"""

from __future__ import annotations

from .edge import EdgeBrowser

__all__ = ["EdgeBrowser"]
