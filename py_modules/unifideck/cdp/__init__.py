"""CDP (Chrome DevTools Protocol) helpers — Steam UI + xCloud control.

OP-13 | py_modules/unifideck/cdp/__init__.py

The plugin uses CDP to drive headless Chromium instances
for two purposes:

* Inject CSS into Steam's frontend (hide "Play"
  buttons on owned-but-Unifideck-launched games);
* Drive Microsoft's xCloud login + token capture inside
  a CDP-controlled Edge instance.

Re-exports the public ``cdp_inject`` surface so callers
can ``from unifideck.cdp import get_cdp_client``. The
optional ``cdp_utils.create_cef_debugging_flag`` import
is wrapped in try/except because the helper is only
present in some builds.
"""

from .cdp_inject import (
    SteamCSSInjector,
    get_cdp_client,
    shutdown_cdp_client,
)

try:
    from .cdp_utils import create_cef_debugging_flag  # noqa: F401
except ImportError:
    pass

__all__ = [
    "SteamCSSInjector",
    "get_cdp_client",
    "shutdown_cdp_client",
]
