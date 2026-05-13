"""Startup-time config validation orchestrator.

OP-10c | py_modules/unifideck/config/startup.py

Single entry point for the bootstrap's config-check
pass. Combines two phases:

1. **Schema validation** (``ConfigValidator``) —
   structural check against ``schema.json``;
2. **Key-presence check** (``collect_missing_keys``) —
   runtime audit ensuring every key the code reads
   is present in ``defaults/config.json``.

Either failure puts the plugin in "degraded" mode
(returned as the second element of the tuple). Degraded
mode means: continue booting, but some features may
use fall-back values. The caller (``boot_plugin``)
exposes this state to the frontend via
``UIHandlers.get_config_validation_status``.

The helpers ``_log_schema_failure`` and
``_log_missing_keys`` write detailed WARN logs so an
operator investigating degraded mode finds actionable
info in plugin logs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from unifideck.config import ConfigValidator, ValidationResult

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)


async def validate_config_at_startup(
    *,
    bus: EventBus,
    config: ConfigManager,
    defaults_path: str,
    user_config_path: str,
) -> tuple[ValidationResult, bool]:
    """Run schema + key-presence validation; return ``(result, degraded_flag)``.

    Two-phase:

    1. ``ConfigValidator.validate_config`` checks
       both layers against ``schema.json``. Failure
       → log + return ``degraded=True`` immediately
       (no point running phase 2 if the schema is
       broken).
    2. ``collect_missing_keys`` checks the live
       ``ConfigManager`` against the
       ``RUNTIME_REQUIRED_KEYS`` list. Any missing
       keys → log + return ``degraded=True``.

    Keyword-only args make the four-collaborator call
    site self-documenting.

    Args:
        bus: live event bus (for validator events).
        config: live ``ConfigManager`` (for key
            presence check).
        defaults_path: bundled defaults file path.
        user_config_path: user override file path.

    Returns:
        ``(ValidationResult, degraded_flag)`` —
        result's ``success`` flag reflects schema
        validation; degraded flag covers both phases.
    """
    validator = ConfigValidator(bus=bus)
    result: ValidationResult = await validator.validate_config(
        defaults_path=defaults_path,
        user_path=user_config_path,
    )
    if not result.success:
        _log_schema_failure(result)
        return result, True
    logger.info(
        "[Unifideck] config validation OK (%d section(s) validated)",
        19,
    )
    from unifideck.config.key_presence import collect_missing_keys

    missing = collect_missing_keys(config)
    if missing:
        _log_missing_keys(missing)
        return result, True
    logger.info("[Unifideck] runtime key-presence check OK")
    return result, False


def _log_schema_failure(result: ValidationResult) -> None:
    """Emit a WARN log summarising the schema-validation failure.

    Shows the first error's path + message inline so
    an operator gets actionable info without enabling
    DEBUG. The full error list is on the
    ``ValidationResult`` for callers that want
    more detail.

    Args:
        result: typed validation result with errors.
    """
    first = result.errors[0] if result.errors else None
    first_path = first.path if first else "<unknown>"
    first_msg = first.message if first else "<unknown>"
    logger.warning(
        "[Unifideck] config validation FAILED — starting in "
        "degraded mode. %d error(s). First: %s: %s",
        len(result.errors),
        first_path,
        first_msg,
    )


def _log_missing_keys(missing: list[str]) -> None:
    """Emit a WARN log listing missing runtime keys (first 10 + overflow).

    Truncates to 10 keys in the log message to keep
    it readable — the full list lives in the
    ``RUNTIME_REQUIRED_KEYS`` source. The "+N more"
    suffix signals overflow clearly.

    The message also documents the fix path: either
    add the key to defaults or remove it from
    RUNTIME_REQUIRED_KEYS if the code stopped
    reading it.

    Args:
        missing: list of dotted keys absent from
            defaults.
    """
    sample = ", ".join(missing[:10])
    overflow = f" (+{len(missing) - 10} more)" if len(missing) > 10 else ""
    logger.warning(
        "[Unifideck] %d runtime-required config key(s) "
        "missing from defaults/config.json: %s%s. "
        "Affected features will run with None values. "
        "Add each key to defaults/config.json or remove "
        "it from RUNTIME_REQUIRED_KEYS if the code no "
        "longer reads it.",
        len(missing),
        sample,
        overflow,
    )
