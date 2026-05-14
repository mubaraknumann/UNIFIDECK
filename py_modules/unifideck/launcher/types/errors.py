"""Launcher exception hierarchy — every error carries a stable ExitCode plus structured context for telemetry."""

from __future__ import annotations
from typing import Any
from .exit_codes import ExitCode
class LauncherError(Exception):
    """Launcher error."""
    exit_code: ExitCode = ExitCode.GENERIC_ERROR
    def __init__(
        self,
        message: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the instance."""
        super().__init__(message)
        self.context: dict[str, Any] = dict(context or {})
    def with_context(self, **fields: Any) -> LauncherError:
        """Merge extra fields into the error's context dict (fluent).

        Args:
            **fields: Key/value pairs to add to ``self.context``
                (existing keys are overwritten).

        Returns:
            ``self`` so the call can be chained inline with the
            raise statement.
        """
        self.context.update(fields)
        return self
    def to_log_dict(self) -> dict[str, Any]:
        """Project the error as a JSON-safe dict for structured logs.

        Returns:
            Dict ``{type, message, exit_code, context}`` ready for
            structured logger extras.
        """
        return {
            "type": type(self).__name__,
            "message": str(self),
            "exit_code": int(self.exit_code),
            "context": self.context,
        }
class LaunchAbortedError(LauncherError):
    """Launch aborted error."""
    exit_code = ExitCode.CANCELLED_BY_USER
class DependencyMissingError(LauncherError):
    """Dependency missing error."""
    exit_code = ExitCode.DEPENDENCY_MISSING
class GameNotFoundError(LauncherError):
    """Game not found error."""
    exit_code = ExitCode.CONFIG_INVALID
class PrefixCorruptedError(LauncherError):
    """Prefix corrupted error."""
    exit_code = ExitCode.PREFIX_CORRUPTED
class ProtonUnavailableError(LauncherError):
    """Proton unavailable error."""
    exit_code = ExitCode.DEPENDENCY_MISSING
class UmuRuntimeError(LauncherError):
    """Umu runtime error."""
    exit_code = ExitCode.GAME_FAILED
class GameFailedError(LauncherError):
    """Game failed error."""
    exit_code = ExitCode.GAME_FAILED
    def __init__(
        self,
        message: str,
        *,
        subprocess_rc: int,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the instance."""
        merged = dict(context or {})
        merged.setdefault("subprocess_rc", subprocess_rc)
        super().__init__(message, context=merged)
        self.subprocess_rc = subprocess_rc