"""GOG.com store implementation.

OP-22-gog | py_modules/unifideck/stores/gog/__init__.py

GOG (Good Old Games) is a DRM-free PC store
implementation that uses ``gogdl`` (the gogdl
CLI) for installs/updates and OAuth via Galaxy's
auth flow for library access.

Submodules:

* ``auth`` — OAuth orchestration;
* ``config`` — config block;
* ``http`` — common HTTP helpers (SSL + JSON
  fetch);
* ``library`` — library reader (Galaxy API);
* ``library_migration`` — version-bump migration
  for the legacy library cache;
* ``store`` — public ``StoreBase`` impl;
* ``updates`` — update probe + download size;
* ``exe_resolver`` — find the real game binary
  in a GOG install;
* ``dlc`` — DLC tracking + apply;
* ``install/`` — install pipeline (planner,
  primitives, installer, marker, progress);
* ``tokens/`` — token persistence + manager.
"""

from .store import GOGStore

__all__ = ["GOGStore"]
