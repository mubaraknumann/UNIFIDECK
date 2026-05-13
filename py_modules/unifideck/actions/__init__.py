"""``unifideck://`` URI dispatcher sub-package.

OP-23 | py_modules/unifideck/actions/__init__.py

Two cooperating modules:

* ``unifideck_uri`` — parser for the
  ``unifideck://[scope/]verb[/arg1/arg2/...]`` URI scheme.
  Produces a ``ParsedAction`` dataclass with the verb,
  scope, args, plus validation flags.
* ``dispatch``      — verb router. Takes a parsed action
  plus the live ``StoreRegistry`` / ``CloudSaveService`` /
  ``SyncService`` collaborators, runs the verb's handler.

The URI scheme is the canonical user-facing action surface:
toast actions, frontend "try again" buttons, even raw
Steam shortcuts all use ``unifideck://`` URIs to invoke
backend logic.

Empty by design: this ``__init__.py`` is a marker — the
public API lives in the two submodules and consumers
import from there directly.
"""
