"""ConfigValidator — schema-based validation of the defaults + user files.

OP-10e | py_modules/unifideck/config/validator.py

Three-phase validation:

1. **Defaults validation** — full strict check
   against ``schema.json``;
2. **User overrides validation** — same schema but
   with ``required`` constraints stripped (user
   overrides are partial; only the keys they
   touch need to validate);
3. **Merged validation** — deep-merge defaults +
   user, re-validate full strict (catches cases
   where overrides break post-merge invariants).

The result carries a typed list of
``ValidationError`` records with source +
JSON-pointer-style path + message, capped at 50 to
keep RPC payloads bounded.

Emits ``CONFIG_VALIDATION_COMPLETED`` or
``CONFIG_VALIDATION_FAILED`` on the bus so observers
(``UIHandlers``, audit) can react.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)

_SCHEMA_PATH = str(Path(__file__).resolve().parent / "schema.json")
_SOURCE_DEFAULTS = "defaults"
_SOURCE_USER = "user_overrides"
_SOURCE_MERGED = "merged"


@dataclass(frozen=True)
class ValidationError:
    """One validation error — source + JSON path + message.

    Frozen so consumers can safely share / hash the
    records.

    Attributes:
        source: ``"defaults"`` / ``"user_overrides"``
            / ``"merged"`` — which phase produced
            the error.
        path: dotted-key path to the offending value.
        message: truncated (256 char) error message.
    """

    source: str
    path: str
    message: str


@dataclass
class ValidationResult:
    """Aggregated validation outcome with per-phase progress flags.

    Attributes:
        success: True iff defaults validated +
            zero errors across all phases.
        errors: typed list (capped at 50).
        defaults_validated: True if phase 1 passed.
        user_overrides_present: True if the user
            file exists (drives the UI
            "user-customised" indicator).
    """

    success: bool = False
    errors: list[ValidationError] = field(default_factory=list)
    defaults_validated: bool = False
    user_overrides_present: bool = False


class ConfigValidator:
    """Three-phase JSON-Schema validation orchestrator."""

    def __init__(self, bus: EventBus | None = None) -> None:
        """Bind the bus (for result events) + initialise the lazy schema cache.

        Schema is loaded on first ``validate_config``
        call and cached for the validator's lifetime.

        Args:
            bus: optional event bus; ``None`` disables
                result events.
        """
        self._bus = bus
        self._schema: dict | None = None

    async def validate_config(
        self,
        defaults_path: str,
        user_path: str | None = None,
    ) -> ValidationResult:
        """Run the three-phase validation and emit a result event.

        Pipeline:

        1. Load schema; on failure register a single
           error + emit + return.
        2. Validate defaults; on failure register
           errors + return (skips remaining phases).
        3. Validate user overrides; on failure
           proceed with defaults-only for the merged
           phase.
        4. Validate the merged dict.
        5. Cap errors at 50, compute ``success``,
           emit result, return.

        Args:
            defaults_path: bundled defaults file.
            user_path: optional user override file
                (skipped if not present).

        Returns:
            Typed ``ValidationResult``.
        """
        result = ValidationResult()
        schema = self._load_schema()
        if schema is None:
            result.errors.append(
                ValidationError(
                    source=_SOURCE_DEFAULTS,
                    path="",
                    message="Cannot load validator schema.json",
                )
            )
            self._emit_result(result)
            return result
        defaults = self._validate_defaults(
            defaults_path,
            schema,
            result,
        )
        if defaults is None:
            self._emit_result(result)
            return result
        merged = self._validate_user_overrides(
            defaults,
            user_path,
            schema,
            result,
        )
        self._validate_merged(merged, schema, result)
        if len(result.errors) > 50:
            result.errors = result.errors[:50]
        result.success = result.defaults_validated and len(result.errors) == 0
        self._emit_result(result)
        return result

    def _validate_defaults(
        self,
        defaults_path: str,
        schema: dict,
        result: ValidationResult,
    ) -> dict | None:
        """Read + validate the defaults file; return the parsed dict or ``None``.

        Two failure points:

        * Read failure → register error + return
          ``None`` (caller short-circuits).
        * Validation failures → register all errors
          + leave ``defaults_validated=False`` but
          return the parsed dict (so the user-
          overrides phase can still run).

        Args:
            defaults_path: file path.
            schema: loaded schema.
            result: aggregator to append to.

        Returns:
            Parsed defaults dict, or ``None`` on read
            failure.
        """
        defaults = self._read_json(defaults_path)
        if defaults is None:
            result.errors.append(
                ValidationError(
                    source=_SOURCE_DEFAULTS,
                    path="",
                    message=f"Cannot read defaults file at {defaults_path}",
                )
            )
            return None
        errors = self._validate_against_schema(
            defaults,
            schema,
            _SOURCE_DEFAULTS,
        )
        result.errors.extend(errors)
        result.defaults_validated = len(errors) == 0
        return defaults

    def _validate_user_overrides(
        self,
        defaults: dict,
        user_path: str | None,
        schema: dict,
        result: ValidationResult,
    ) -> dict:
        """Validate user overrides against a ``required``-relaxed schema.

        Three-arm logic:

        * No user path or file missing → return
          defaults unchanged (no overrides).
        * Read failure → register error + return
          defaults.
        * Validation errors → register, return
          defaults (don't merge invalid overrides).
        * Success → deep-merge + return merged.

        ``required`` constraints are stripped from
        the schema for this phase because user
        overrides are partial.

        Args:
            defaults: parsed defaults dict.
            user_path: optional user file path.
            schema: loaded schema.
            result: aggregator.

        Returns:
            Dict for the merged phase (defaults or
            merged).
        """
        if user_path is None:
            return defaults
        expanded = str(Path(user_path).expanduser())
        if not Path(expanded).is_file():
            return defaults
        result.user_overrides_present = True
        user_data = self._read_json(expanded)
        if user_data is None:
            result.errors.append(
                ValidationError(
                    source=_SOURCE_USER,
                    path="",
                    message=(f"Cannot read or parse user overrides at {expanded}"),
                )
            )
            return defaults
        relaxed_schema = self._strip_required(schema)
        user_errors = self._validate_against_schema(
            user_data,
            relaxed_schema,
            _SOURCE_USER,
        )
        result.errors.extend(user_errors)
        if user_errors:
            return defaults
        return self._deep_merge(defaults, user_data)

    @staticmethod
    def _strip_required(schema: Any) -> Any:
        """Recursively remove every ``required`` key from a JSON-Schema tree.

        Handles dicts (drop ``required`` key, recurse
        on values) + lists (recurse on items). Other
        types pass through unchanged.

        Non-mutating: builds and returns a fresh
        tree. The original schema isn't touched
        (callers reuse it for the merged phase).

        Args:
            schema: JSON-Schema tree.

        Returns:
            Required-free copy.
        """
        if isinstance(schema, dict):
            out = {}
            for key, value in schema.items():
                if key == "required":
                    continue
                out[key] = ConfigValidator._strip_required(value)
            return out
        if isinstance(schema, list):
            return [ConfigValidator._strip_required(item) for item in schema]
        return schema

    def _validate_merged(
        self,
        merged: dict,
        schema: dict,
        result: ValidationResult,
    ) -> None:
        """Validate the deep-merged dict, skipped if earlier phases already failed.

        Two skip conditions:

        * Defaults didn't validate → merged check
          is pointless (would re-surface same errors);
        * User overrides had errors → defaults are
          unchanged, merged check would duplicate
          defaults validation.

        Otherwise full strict check.

        Args:
            merged: deep-merged dict.
            schema: loaded schema.
            result: aggregator.
        """
        if not result.defaults_validated:
            return
        if any(e.source == _SOURCE_USER for e in result.errors):
            return
        merged_errors = self._validate_against_schema(
            merged,
            schema,
            _SOURCE_MERGED,
        )
        result.errors.extend(merged_errors)

    def _load_schema(self) -> dict | None:
        """Lazy-load + cache the validator schema, returning ``None`` on failure.

        Schema lives at
        ``<this_module>/schema.json``. Load failure
        logs at ERROR and returns ``None`` — callers
        register a top-level error and stop.

        Returns:
            Parsed schema dict or ``None``.
        """
        if self._schema is not None:
            return self._schema
        try:
            with Path(_SCHEMA_PATH).open(encoding="utf-8") as f:
                self._schema = json.load(f)
            return self._schema
        except (OSError, json.JSONDecodeError) as e:
            logger.error(
                "[ConfigValidator] cannot load schema at %s: %s",
                _SCHEMA_PATH,
                e,
            )
            return None

    @staticmethod
    def _read_json(path: str) -> dict | None:
        """Read + parse a JSON file, returning ``None`` on any failure.

        Failure modes:

        * OSError on open;
        * JSONDecodeError on parse;
        * Non-dict root (defensive — top-level array
          isn't a valid config layer).

        All logged at WARN.

        Args:
            path: file path (``~`` is expanded).

        Returns:
            Parsed dict or ``None``.
        """
        expanded = str(Path(path).expanduser())
        try:
            with Path(expanded).open(encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "[ConfigValidator] cannot read %s: %s",
                expanded,
                e,
            )
            return None
        if not isinstance(data, dict):
            logger.warning(
                "[ConfigValidator] %s is not a JSON object",
                expanded,
            )
            return None
        return data

    @staticmethod
    def _validate_against_schema(
        data: dict,
        schema: dict,
        source: str,
    ) -> list[ValidationError]:
        """Run jsonschema Draft7 validation and return typed error records.

        Two paths:

        * ``jsonschema`` not installed → log at
          ERROR + return a single error record
          flagging the missing dep. Validation is
          effectively skipped but the result captures
          the cause for diagnostics.
        * Library available → iterate ``iter_errors``,
          convert each error's ``absolute_path`` to
          a dotted string, truncate the message to
          256 chars.

        Args:
            data: dict to validate.
            schema: loaded schema.
            source: phase tag for the error records.

        Returns:
            List of ``ValidationError`` (possibly
            empty).
        """
        try:
            import jsonschema
        except ImportError:
            logger.error(
                "[ConfigValidator] jsonschema not installed — validation skipped",
            )
            return [
                ValidationError(
                    source=source,
                    path="",
                    message="jsonschema library not installed",
                )
            ]
        errors: list[ValidationError] = []
        validator = jsonschema.Draft7Validator(schema)
        for err in validator.iter_errors(data):
            path = ".".join(str(p) for p in err.absolute_path)
            errors.append(
                ValidationError(
                    source=source,
                    path=path,
                    message=err.message[:256],
                )
            )
        return errors

    @staticmethod
    def _deep_merge(base: dict, overrides: dict) -> dict:
        """Deep-merge ``overrides`` over ``base`` (same semantics as ConfigManager).

        Recursive on nested dicts; override wins on
        scalar conflicts.

        Args:
            base: lower-priority dict.
            overrides: higher-priority dict.

        Returns:
            New merged dict (non-mutating).
        """
        result = dict(base)
        for key, value in overrides.items():
            if (
                key in result
                and isinstance(result[key], dict)
                and isinstance(value, dict)
            ):
                result[key] = ConfigValidator._deep_merge(
                    result[key],
                    value,
                )
            else:
                result[key] = value
        return result

    def _emit_result(self, result: ValidationResult) -> None:
        """Schedule a ``CONFIG_VALIDATION_*`` event on the bus.

        Fire-and-forget (uses ``create_task``) so
        the validator doesn't block on subscribers.
        Three guards:

        * ``bus is None`` → skip;
        * No running loop → skip (sync context);
        * Any RuntimeError / CancelledError on emit
          → DEBUG log + swallow.

        Success path emits
        ``CONFIG_VALIDATION_COMPLETED`` with
        progress flags; failure path emits
        ``CONFIG_VALIDATION_FAILED`` with error
        count + first-error metadata for quick
        triage in the UI.

        Args:
            result: typed validation result.
        """
        if self._bus is None:
            return
        try:
            import asyncio
            from ..core.types.events import Events

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            if result.success:
                loop.create_task(
                    self._bus.emit(
                        Events.CONFIG_VALIDATION_COMPLETED,
                        defaults_validated=result.defaults_validated,
                        user_overrides_present=result.user_overrides_present,
                    )
                )
            else:
                loop.create_task(
                    self._bus.emit(
                        Events.CONFIG_VALIDATION_FAILED,
                        error_count=len(result.errors),
                        defaults_validated=result.defaults_validated,
                        user_overrides_present=result.user_overrides_present,
                        first_error_source=(
                            result.errors[0].source if result.errors else ""
                        ),
                        first_error_path=(
                            result.errors[0].path if result.errors else ""
                        ),
                    )
                )
        except (RuntimeError, asyncio.CancelledError) as e:
            logger.debug(
                "[ConfigValidator] event emit failed: %s",
                e,
            )
