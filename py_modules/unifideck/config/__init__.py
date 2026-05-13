"""Config layer — defaults + user override + schema validation.

OP-10 | py_modules/unifideck/config/__init__.py

The plugin reads configuration from two JSON files:

* ``defaults/config.json`` — bundled with the plugin,
  documents every supported key + default;
* ``~/.config/unifideck/config.json`` (user override) —
  user-customised values, merged over the defaults.

Public surface:

* ``ConfigManager`` — merge-aware reader with dotted-key
  lookup;
* ``load_json_layer`` / ``atomic_write_json`` —
  persistence primitives;
* ``validate_i18n_schema`` — strict validation of the
  i18n section (delegated to ``scripts/locale_config``);
* ``ConfigValidator`` + ``ValidationResult`` —
  general-purpose schema validation against
  ``schema.json``.

``startup.py`` (not re-exported here) glues these
together at boot — see ``unifideck.bootstrap.boot``.
"""

from .config_manager import ConfigManager
from .config_persistence import (
    atomic_write_json,
    load_json_layer,
)
from .i18n_schema import (
    ConfigSchemaError,
    validate_i18n_schema,
)
from .validator import (
    ConfigValidator,
    ValidationError,
    ValidationResult,
)

__all__ = [
    "ConfigManager",
    "load_json_layer",
    "atomic_write_json",
    "validate_i18n_schema",
    "ConfigSchemaError",
    "ConfigValidator",
    "ValidationResult",
    "ValidationError",
]
