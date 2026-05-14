"""Launcher entry point — parses argv, builds the LaunchContext, and dispatches to the appropriate flow (native, xcloud, auth)."""

from __future__ import annotations
import asyncio
import logging
import os
import sys
from pathlib import Path
from ..core.types.results import Result
from .types.context import LaunchContext
from .types.errors import GameNotFoundError, LauncherError
from .types.exit_codes import ExitCode
logger = logging.getLogger(__name__)
def _parse_argv(argv: list[str]) -> tuple[str, str]:
    """Parse the command-line argv into ``(game_key, raw_options)``.

    Args:
        argv: Process argv. ``argv[1]`` must be of the form
            ``"store:game_id"``; ``argv[2:]`` is joined into a
            single raw options string.

    Returns:
        Tuple ``(game_key, raw_options)``.

    Raises:
        GameNotFoundError: ``argv[1]`` missing or malformed (no colon).
    """
    if len(argv) < 2:
        raise GameNotFoundError(
            "missing store:game_id argument",
            context={"argv": argv},
        )
    game_key = argv[1]
    if ":" not in game_key:
        raise GameNotFoundError(
            f"malformed game key {game_key!r}, "
            "expected 'store:game_id'",
            context={"game_key": game_key},
        )
    raw_options = " ".join(argv[2:])
    return game_key, raw_options
def _resolve_plugin_dir() -> Path:
    """Resolve the plugin root directory by walking up from this file.

    Returns:
        Absolute ``Path`` to the plugin root.
    """
    from ..core.paths import resolve_plugin_dir
    return resolve_plugin_dir(start=Path(__file__))
async def _build_context(
    argv: list[str],
    shortcut_svc,
) -> LaunchContext:
    """Build a ``LaunchContext`` for the requested game.

    Parses argv, looks the entry up in the shortcut service's
    games map, then resolves auth-action and circuit-breaker
    bypass state from the environment.

    Args:
        argv: Process argv (passed to ``_parse_argv``).
        shortcut_svc: Shortcut service exposing
            ``get_entry_for_game_key``.

    Returns:
        Fully-populated ``LaunchContext``.

    Raises:
        GameNotFoundError: argv malformed or the game key is
            missing from the games map.
    """
    game_key, raw_options = _parse_argv(argv)
    store, game_id = game_key.split(":", 1)
    entry = await shortcut_svc.get_entry_for_game_key(
        store, game_id,
    )
    if entry is None:
        raise GameNotFoundError(
            f"game {game_key!r} not found in games.map",
            context={"game_key": game_key},
        )
    exe_path = Path(entry.exe)
    work_dir = Path(entry.work_dir)
    auth_store, is_launch_action = _detect_auth_action()
    bypass = _resolve_bypass_flag(store, game_id)
    return LaunchContext(
        store=store,
        game_id=game_id,
        exe_path=exe_path,
        work_dir=work_dir,
        plugin_dir=_resolve_plugin_dir(),
        raw_options=raw_options,
        is_launch_action=is_launch_action,
        auth_store=auth_store,
        bypass_circuit_breaker=bypass,
    )

def _detect_auth_action() -> tuple[str | None, bool]:

    """Detect whether the current invocation is an auth flow.

    Reads the ``UNIFIDECK_<STORE>_ACTION`` env vars (epic/gog/amazon).
    If any is set to ``"auth"``, returns that store name and
    ``is_launch_action=False``; otherwise returns
    ``(None, True)`` for a normal launch.

    Returns:
        Tuple ``(auth_store, is_launch_action)``.
    """
    auth_env = {
        "epic": os.environ.get("UNIFIDECK_EPIC_ACTION"),
        "gog": os.environ.get("UNIFIDECK_GOG_ACTION"),
        "amazon": os.environ.get("UNIFIDECK_AMAZON_ACTION"),
    }
    for candidate_store, action in auth_env.items():
        if action == "auth":
            return candidate_store, False
    return None, True
def _resolve_bypass_flag(store: str, game_id: str) -> bool:
    """Resolve whether the circuit breaker should be bypassed for this run.

    True if either the ``UNIFIDECK_BYPASS_CIRCUIT_BREAKER`` env var
    is set to a truthy value, or the launch history service has a
    one-shot bypass queued for this ``store:game_id`` pair.

    Args:
        store: Store identifier (e.g. ``"epic"``).
        game_id: Per-store game identifier.

    Returns:
        True iff the breaker should be bypassed.
    """
    bypass_raw = os.environ.get(
        "UNIFIDECK_BYPASS_CIRCUIT_BREAKER", "",
    )
    bypass_env = bypass_raw.strip().lower() in (
        "1", "true", "yes",
    )
    try:
        from ..config.config_manager import ConfigManager
        from ..services.launch_history import (
            LaunchHistoryService,
        )
        cfg = ConfigManager(
            str(
                _resolve_plugin_dir()
                / "defaults" / "config.json",
            ),
        )
        lh = LaunchHistoryService(cfg)
        bypass_flag = lh.consume_bypass(f"{store}:{game_id}")
    except Exception:
        bypass_flag = False
    return bypass_env or bypass_flag
async def _run(argv: list[str]) -> int:
    """Top-level run entry — wraps ``_run_with_id`` with launch ID
    correlation and per-launch log archival.

    Generates a fresh launch ID, attaches the archive log handler,
    prunes old launches, then defers to ``_run_with_id``.

    Args:
        argv: Process argv.

    Returns:
        Exit code suitable for ``sys.exit``.
    """
    from .diagnostics.correlation import launch_id_scope, new_launch_id
    from .diagnostics.log_archive import (
        attach_launch_handler,
        detach_launch_handler,
        prune_old_launches,
    )
    lid = new_launch_id()
    with launch_id_scope(lid):
        try:
            from ..config.config_manager import ConfigManager
            from ..core.paths import resolve_plugin_dir
            _cfg = ConfigManager(str(
                resolve_plugin_dir() /
                "defaults" /
                "config.json"))
        except Exception:
            _cfg = None
        prune_old_launches(_cfg)
        _archive_handler = attach_launch_handler(lid, _cfg)
        try:
            return await _run_with_id(argv)
        finally:
            detach_launch_handler(_archive_handler)

async def _run_with_id(argv: list[str]) -> int:

    """Execute a single launch attempt under an already-set launch ID.

    Steps: parse argv → bootstrap minimal services → build context
    → start service → launch → stop service. Each LauncherError
    is mapped to its declared ``exit_code``.

    Args:
        argv: Process argv.

    Returns:
        Exit code (``ExitCode`` IntEnum value as int).
    """
    try:
        game_key, _ = _parse_argv(argv)
        logger.info(
            "[launcher.dispatcher] request received: %s", game_key,
        )
    except LauncherError as err:
        logger.error(
            "[launcher.dispatcher] argv parse failed: %s",
            err.to_log_dict,
        )
        return int(err.exit_code)
    try:
        launcher_service = _bootstrap_minimal_services()
    except Exception:
        logger.exception("[launcher.dispatcher] bootstrap failed")
        return int(ExitCode.DEPENDENCY_MISSING)
    try:
        ctx = await _build_context(argv, launcher_service._shortcut_svc)
    except LauncherError as err:
        logger.error(
            "[launcher.dispatcher] context build failed: %s",
            err.to_log_dict,
        )
        return int(err.exit_code)
    try:
        await launcher_service.start()
        result = await launcher_service.launch(ctx)
    except LauncherError as err:
        logger.error(
            "[launcher.dispatcher] launch raised: %s",
            err.to_log_dict,
        )
        return int(err.exit_code)
    finally:
        try:
            await launcher_service.stop()
        except Exception:
            logger.exception(
                "[launcher.dispatcher] launcher_service.stop failed",
            )
    return _map_result_to_exitcode(result)
def _map_result_to_exitcode(result: Result) -> int:
    """Translate a launch ``Result`` into a process exit code.

    Recognized ``error_code`` values:
      * ``"not_implemented"`` → ``GENERIC_ERROR``
      * ``"circuit_open"``    → ``CIRCUIT_BREAKER_OPEN``
      * ``"exit_<N>"``        → ``N`` (clamped to 0–255)
    Anything else, or success, maps to ``GAME_FAILED`` / ``SUCCESS``.

    Args:
        result: The launch result.

    Returns:
        Integer exit code in [0, 255].
    """
    if result.success:
        return int(ExitCode.SUCCESS)
    code = result.error_code or ""
    if code == "not_implemented":
        return int(ExitCode.GENERIC_ERROR)
    if code == "circuit_open":
        return int(ExitCode.CIRCUIT_BREAKER_OPEN)
    if code.startswith("exit_"):
        try:
            rc = int(code.split("_", 1)[1])
            return rc if 0 <= rc <= 255 else int(ExitCode.GAME_FAILED)
        except (ValueError, IndexError):
            return int(ExitCode.GAME_FAILED)
    return int(ExitCode.GAME_FAILED)
def _bootstrap_minimal_services():
    """Build the minimal service graph required to launch a game.

    Delegates to ``launcher.bootstrap.build_launcher_service``.

    Returns:
        A wired ``LauncherService`` instance.
    """
    from .bootstrap import build_launcher_service
    return build_launcher_service()

def main(argv: list[str]) -> int:

    """Process entry point — configures logging then runs the dispatcher.

    Installs the launch-ID logging filter, runs ``_run`` under
    ``asyncio.run``, and converts uncaught exceptions /
    ``KeyboardInterrupt`` / ``CancelledError`` into the matching
    exit codes.

    Args:
        argv: Process argv (typically ``sys.argv``).

    Returns:
        Exit code in [0, 255].
    """
    from .diagnostics.correlation import install_launch_id_logging
    logging.basicConfig(
        format=(
            "%(asctime)s [%(launch_id)s] %(levelname)s "
            "%(name)s: %(message)s"
        ),
        level=logging.INFO,
        stream=sys.stderr,
    )
    install_launch_id_logging()
    try:
        return int(asyncio.run(_run(argv)))
    except KeyboardInterrupt:
        return int(ExitCode.CANCELLED_BY_USER)
    except asyncio.CancelledError:
        logger.info(
            "[launcher.dispatcher] launch cancelled by user",
        )
        return int(ExitCode.CANCELLED_BY_USER)
    except Exception:
        logger.exception("[launcher.dispatcher] uncaught exception")
        return int(ExitCode.GENERIC_ERROR)
if __name__ == "__main__":
    sys.exit(main(sys.argv))