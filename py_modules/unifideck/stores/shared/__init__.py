"""Shared infrastructure for store implementations.

OP-25-shared
File: py_modules/unifideck/stores/shared/__init__.py

Re-exports the core scaffolding every concrete
store relies on:

* ``StoreBase`` — abstract base class with the
  contract every store implements.
* ``StoreRegistry`` — central registry for
  registering, looking up, and operating on
  stores.
* ``cli_install_helpers`` — async helpers for
  draining CLI subprocess output, timeouts, and
  progress parsing.
* ``dlc`` — DLC-flag helpers for stores that
  support DLC installation through their CLI.
"""

from . import cli_install_helpers, dlc
from .store_base import StoreBase
from .store_registry import StoreRegistry

__all__ = [
    "StoreBase",
    "StoreRegistry",
    "cli_install_helpers",
    "dlc",
]
