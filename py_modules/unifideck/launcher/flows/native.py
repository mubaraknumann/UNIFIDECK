"""Native Linux launch flow — runs the game binary directly under the Steam runtime when no Proton prefix is required."""

from __future__ import annotations
import asyncio
import logging
import os
from pathlib import Path
from ...core.types import Result
from ..types.context import LaunchContext, RuntimeState
from ..types.errors import DependencyMissingError, GameFailedError
logger = logging.getLogger(__name__)
STEAM_RUNTIME_CANDIDATES = [
    "~/.steam/steam/ubuntu12_32/steam-runtime/run.sh",
    "~/.local/share/Steam/ubuntu12_32/steam-runtime/run.sh",
]
def _find_steam_runtime() -> Path | None:
    """Locate the Steam Runtime ``run.sh`` under the user's home.

    Returns:
        Path to ``run.sh``, or ``None`` if no known location
        holds it.
    """
    for candidate in STEAM_RUNTIME_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
    return None
def _restore_steam_env(env: dict[str, str]) -> None:
    """Copy STEAM_OVERLAY / STEAM_INPUT from ``~/.steam/steam.env``.

    Steam strips these from its environment before invoking
    non-Steam shortcuts; re-injecting them here lets Steam's
    overlay and input remap work for our games.

    Args:
        env: Environment dict to update in-place.
    """
    steam_env = Path("~/.steam/steam.env").expanduser()
    if not steam_env.is_file():
        return
    try:
        for line in steam_env.read_text(
            encoding="utf-8", errors="replace",
        ).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key in ("STEAM_OVERLAY", "STEAM_INPUT"):
                env[key] = value
    except OSError:
        pass
def _is_gog_dosbox_wrapper(ctx: LaunchContext) -> bool:
    """Return True iff this context points at a GOG DOSBox ``start.sh``.

    Args:
        ctx: Launch context.

    Returns:
        True if the store is GOG and the exe name is ``start.sh``.
    """
    return (
        ctx.store == "gog"
        and ctx.exe_path.name == "start.sh"
    )

async def native_launch(
    ctx: LaunchContext,
    state: RuntimeState,
) -> Result:

    """Spawn a Linux-native game binary (optionally under the Steam Runtime).

    GOG ``start.sh`` games are routed through the in-tree
    ``gog_linux_dosbox`` module so we get the bundled DOSBox.
    Other native games go through ``run.sh`` if it can be
    located, else direct exec.

    Args:
        ctx: Launch context.
        state: Runtime state (wrappers + game args).

    Returns:
        A ``Result`` on game exit 0.

    Raises:
        DependencyMissingError: Game exe not found.
        GameFailedError: Game exited non-zero.
    """
    exe_path = ctx.exe_path
    if not exe_path.is_file():
        raise DependencyMissingError(
            f"Native Linux executable not found: {exe_path}",
            context={"exe": str(exe_path), "store": ctx.store},
        )
    try:
        os.chmod(exe_path, 0o755)
    except OSError:
        pass
    env = _prepare_launch_env(ctx)
    argv = _build_launch_argv(ctx, state, exe_path)
    cwd = ctx.work_dir if ctx.work_dir.is_dir() else exe_path.parent
    logger.info(
        "[launcher.native] spawning: argv=%s cwd=%s",
        argv[:3],
        cwd,
    )
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        cwd=str(cwd),
        stdout=None,
        stderr=None,
        start_new_session=True,
    )
    rc = await proc.wait()
    state.game_exit_code = rc
    logger.info("[launcher.native] game exited rc=%d", rc)
    if rc != 0:
        raise GameFailedError(
            f"Native Linux game exited with code {rc}",
            subprocess_rc=rc,
            context={
                "store": ctx.store,
                "game_id": ctx.game_id,
            },
        )
    return Result(success=True, store=ctx.store)
def _prepare_launch_env(ctx: LaunchContext) -> dict[str, str]:
    """Build the environment for the native game subprocess.

    Starts from the current environment, applies user env
    overrides from the launch context, then restores
    STEAM_OVERLAY / STEAM_INPUT from steam.env.

    Args:
        ctx: Launch context.

    Returns:
        Environment dict ready for the subprocess.
    """
    env = dict(os.environ)
    env.update(ctx.env_overrides)
    _restore_steam_env(env)
    return env

def _build_launch_argv(
    ctx: LaunchContext,
    state: RuntimeState,
    exe_path: Path,
) -> list[str]:

    """Build the argv for the native game subprocess.

    Composition: ``state.wrappers`` + (DOSBox-wrapper module |
    Steam Runtime | direct exec) + ``state.game_args``.

    Args:
        ctx: Launch context.
        state: Runtime state.
        exe_path: Resolved exe path.

    Returns:
        argv list.
    """
    argv: list[str] = list(state.wrappers)
    if _is_gog_dosbox_wrapper(ctx):
        logger.info(
            "[launcher.native] using GOG DOSBox wrapper module",
        )
        argv.extend([
            "python3", "-m",
            "unifideck.launcher.proton.gog_linux_dosbox",
            str(exe_path),
        ])
    else:
        runtime = _find_steam_runtime()
        if runtime is not None:
            logger.info(
                "[launcher.native] using Steam Runtime: %s", runtime,
            )
            argv.extend([str(runtime), str(exe_path)])
        else:
            logger.info(
                "[launcher.native] no Steam Runtime, direct exec",
            )
            argv.append(str(exe_path))
    argv.extend(state.game_args)
    return argv