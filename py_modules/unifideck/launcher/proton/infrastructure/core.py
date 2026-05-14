"""launcher/proton/infrastructure/core.py — Shared Proton/UMU launch setup."""
from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...types.context import LaunchContext, RuntimeState
from ...types.errors import DependencyMissingError

logger = logging.getLogger(__name__)

STORE_TO_UMU = {
    "epic": "egs",
    "gog": "gog",
    "amazon": "amazon",
    "ubisoft": "ubisoft",
    "microsoft": "microsoft",
}


@dataclass(frozen=True)
class ProtonLaunchPlan:
    """Everything store handlers need to spawn umu-run."""
    context: LaunchContext
    state: RuntimeState
    python_bin: Path
    umu_wrapper: Path
    prefix_path: Path
    env: dict[str, str]
    on_process_start: Callable[[object], None] | None = None
def _ubisoft_prefix_path(ctx: LaunchContext, prefixes_dir: Path) -> Path:
    """Build the per-game prefix path for a Ubisoft launch.

    Honors the ``UNIFIDECK_UBISOFT_PREFIX_NAME`` env override
    to allow several Ubisoft titles to share one prefix (a
    common requirement when one game's installer also seeds
    another's data).

    Args:
        ctx: Launch context.
        prefixes_dir: Base ``~/.local/share/unifideck/prefixes``.

    Returns:
        Path ``<prefixes_dir>/ubisoft/<name>``.
    """
    import os
    ubi_name = os.environ.get("UNIFIDECK_UBISOFT_PREFIX_NAME") or ctx.game_id
    return prefixes_dir / "ubisoft" / ubi_name
def _resolve_prefix(ctx: LaunchContext) -> Path:
    """Pick (and create) the Wine prefix path for a launch.

    Ubisoft uses a dedicated path resolved by
    ``_ubisoft_prefix_path``. All other stores get
    ``<prefixes_dir>/<game_id>`` with any trailing ``pfx``
    stripped.

    Args:
        ctx: Launch context.

    Returns:
        Resolved prefix path (parent dirs created).
    """
    prefixes_dir = Path("~/.local/share/unifideck/prefixes").expanduser()
    if ctx.store == "ubisoft":
        path = _ubisoft_prefix_path(ctx, prefixes_dir)
    else:
        path = prefixes_dir / ctx.game_id
        while path.name == "pfx":
            path = path.parent
    path.mkdir(parents=True, exist_ok=True)
    return path
def _lookup_umu_id(
 ctx: LaunchContext,
 umu_store: str,
 plugin_dir: Path,
) -> str | None:
    """Resolve the UMU game ID via the bundled ``umu_lookup.py`` helper.

    Args:
        ctx: Launch context.
        umu_store: UMU store code (``egs``, ``gog``, …).
        plugin_dir: Plugin root directory.

    Returns:
        UMU ID string, or ``None`` if the helper is missing,
        times out, or fails.
    """
    helper = plugin_dir / "bin" / "umu_lookup.py"
    if not helper.is_file():
        return None
    try:
        out = subprocess.check_output(
            ["python3", str(helper), ctx.game_id, umu_store],
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        text = out.decode().strip()
        return text or None
    except (subprocess.SubprocessError, OSError):
        return None

def _locate_umu_wrapper(proton_path: Path) -> Path:

    """Locate the ``umu-run`` wrapper next to Proton or on PATH.

    Args:
        proton_path: Path to the resolved Proton script.

    Returns:
        Path to ``umu-run``.

    Raises:
        DependencyMissingError: Neither bundled nor system
            ``umu-run`` could be found.
    """
    bundled = proton_path.parent / "umu-run"
    if bundled.is_file():
        return bundled
    system = shutil.which("umu-run")
    if system:
        return Path(system)
    raise DependencyMissingError(
        "umu-run not found (neither bundled with proton "
        "nor in PATH)",
        context={"proton_path": str(proton_path)},
    )
def proton_prepare(
 ctx: LaunchContext,
 state: RuntimeState,
 *,
 python_bin: Path,
 proton_path: Path,
 proton_tool_id: str,
 on_process_start: Callable[[object], None] | None = None,
) -> ProtonLaunchPlan:
    """Assemble the ``ProtonLaunchPlan`` consumed by per-store handlers.

    Resolves the UMU store code + game ID, the Wine prefix,
    the umu-run wrapper, and builds the subprocess environment
    (GAMEID, STORE, STEAM_COMPAT_*, PROTON_VERB) merged with
    user env overrides. Also fills ``state`` for downstream
    telemetry.

    Args:
        ctx: Launch context.
        state: Runtime state (mutated).
        python_bin: Python interpreter to feed umu-run.
        proton_path: Resolved Proton script path.
        proton_tool_id: Proton tool identifier (for state).
        on_process_start: Optional callback invoked once
            the umu-run subprocess starts.

    Returns:
        Ready-to-use ``ProtonLaunchPlan``.
    """
    import os
    umu_store = STORE_TO_UMU.get(ctx.store, "none")
    prefix_path = _resolve_prefix(ctx)
    umu_id = _lookup_umu_id(ctx, umu_store, ctx.plugin_dir)
    umu_wrapper = _locate_umu_wrapper(proton_path)
    state.python_bin = python_bin
    state.proton_path = proton_path
    state.proton_tool_id = proton_tool_id
    state.prefix_path = prefix_path
    state.umu_store_code = umu_store
    state.umu_id = umu_id
    state.umu_wrapper = umu_wrapper
    env = dict(os.environ)
    env["GAMEID"] = umu_id or "umu-0"
    env["STORE"] = umu_store
    env["STEAM_COMPAT_DATA_PATH"] = str(prefix_path)
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(
    Path("~/.steam/root").expanduser,
   )
    env["PROTON_VERB"] = "waitforexitandrun"
    env.update(ctx.env_overrides)
    logger.info(
    "[launcher.proton.core] plan ready: store=%s umu_store=%s "
    "umu_id=%s prefix=%s proton=%s",
    ctx.store, umu_store, umu_id, prefix_path, proton_tool_id,
   )
    return ProtonLaunchPlan(
        context=ctx,
        state=state,
        python_bin=python_bin,
        umu_wrapper=umu_wrapper,
        prefix_path=prefix_path,
        env=env,
        on_process_start=on_process_start,
    )
