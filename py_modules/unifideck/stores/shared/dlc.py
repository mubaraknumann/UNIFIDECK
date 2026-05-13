"""DLC-flag helpers used by the install pipeline for store CLIs that support DLCs.

OP-25-shared-dlc
File: py_modules/unifideck/stores/shared/dlc.py

Two stores in our supported list (Epic via
legendary, GOG via gogdl) accept a ``--with-dlcs``
flag on their CLI's ``install`` command that pulls
all DLCs alongside the base game. The other stores
(Amazon's nile, Microsoft's xCloud, Ubisoft's
in-house tooling) either don't have DLC bundling at
the CLI level or don't have a CLI at all, so the
helpers here simply emit nothing for them.

This module exists so the install orchestrator can
ask "should we add DLC flags?" without hard-coding
store names anywhere else.
"""

import logging

logger = logging.getLogger(__name__)

_DLC_SUPPORTED_STORES = {"epic", "gog"}


def get_dlc_flags(store: str) -> list[str]:
    """Return the CLI args that enable DLC installation for ``store``.

    Case-insensitive lookup against the
    ``_DLC_SUPPORTED_STORES`` set. Returns an empty
    list for unsupported stores so callers can
    unconditionally ``argv.extend(get_dlc_flags(...))``
    without branching.

    Args:
        store: store name (any case).

    Returns:
        ``["--with-dlcs"]`` for Epic/GOG, ``[]``
        otherwise.
    """
    if store.lower() in _DLC_SUPPORTED_STORES:
        return ["--with-dlcs"]
    return []


def store_supports_dlc(store: str) -> bool:
    """Check whether ``store`` supports DLC installation through its CLI.

    Boolean variant of ``get_dlc_flags`` — used by
    UI code that needs to gate a "include DLCs"
    checkbox.

    Args:
        store: store name (any case).

    Returns:
        True for Epic/GOG.
    """
    return store.lower() in _DLC_SUPPORTED_STORES
