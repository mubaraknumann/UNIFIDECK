"""epic_prerequisites.py — Run per-game prerequisite installers before legendary launch.

# OP-44f | py_modules/unifideck/launcher/proton/fixes/epic_prerequisites.py | Depends: (none)

Some Epic titles ship a ``prereq_info`` block in legendary's
``installed.json`` pointing at a Windows installer (Ubisoft Connect
bootstrap, Visual C++ redistributables, …) that must run inside the
Wine prefix before the game itself launches. Mirrors Heroic's
``legendarySetup`` behaviour. Runs once per (game_id, prefix) pair —
guarded by a marker file inside the prefix so subsequent launches are
zero-cost.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from ..infrastructure.core import ProtonLaunchPlan

logger = logging.getLogger(__name__)
_INSTALLER_TIMEOUT_S = 600
_LEGENDARY_CONFIG_CANDIDATES = (
    Path('~/.config/legendary'),
    Path(
        '~/.var/app/com.heroicgameslauncher.hgl/config/heroic/'
        'legendaryConfig/legendary',
    ),
)


async def apply_epic_prerequisites(plan: ProtonLaunchPlan) -> bool:
    """Apply EPIC prerequisites.

    Returns True when the prereq step is satisfied (already done, no
    prereqs defined, or installer succeeded). Returns False only when
    a prereq exists and its installer failed — the caller may surface
    this but typically continues anyway, mirroring the legacy bash
    helper.
    """
    game_id = plan.context.game_id
    prefix_root = _normalize_prefix_root(plan.prefix_path)
    new_marker, legacy_marker = _get_marker_paths(game_id, prefix_root)
    if new_marker.is_file() or legacy_marker.is_file():
        logger.debug(
            '[epic_prerequisites] %s: marker present, skipping',
            game_id,
        )
        return True
    prereq = _get_prereq_info(game_id)
    if prereq is None:
        _write_marker_sync(new_marker, body='no prerequisites')
        _cleanup_legacy_marker(legacy_marker)
        logger.info(
            '[epic_prerequisites] %s: no prereqs defined', game_id,
        )
        return True
    name = prereq.get('name') or 'unknown'
    logger.info(
        '[epic_prerequisites] %s: installing %s', game_id, name,
    )
    ok = await _run_prerequisite(plan, prereq, prefix_root)
    if ok:
        _write_marker_sync(new_marker, body=f'installed: {name}')
        _cleanup_legacy_marker(legacy_marker)
        logger.info(
            '[epic_prerequisites] %s: %s installed', game_id, name,
        )
    else:
        logger.warning(
            '[epic_prerequisites] %s: %s install failed', game_id, name,
        )
    return ok


def _normalize_prefix_root(prefix_path: Path) -> Path:
    """Strip trailing ``pfx`` segments to return the canonical prefix root.

    Args:
        prefix_path: Any path inside (or equal to) the Wine prefix.

    Returns:
        The resolved prefix root.
    """
    p = prefix_path.resolve()
    while p.name == 'pfx':
        p = p.parent
    return p


def _get_marker_paths(game_id: str, prefix_root: Path) -> tuple[Path, Path]:
    """Compute the prereq-done marker filenames inside the prefix.

    The new marker incorporates a 12-char SHA-1 hash of the
    prefix path so a renamed/moved prefix doesn't inherit a
    stale completion marker. Legacy marker (no hash) is also
    returned so callers can clean it up.

    Args:
        game_id: Epic Games game identifier.
        prefix_root: Canonical prefix root.

    Returns:
        Tuple ``(new_marker, legacy_marker)``.
    """
    prefix_hash = hashlib.sha1(
        str(prefix_root).encode('utf-8'),
    ).hexdigest()[:12]
    new_marker = prefix_root / (
        f'.unifideck_prereqs_{game_id}_{prefix_hash}.done'
    )
    legacy_marker = prefix_root / f'.unifideck_prereqs_{game_id}.done'
    return new_marker, legacy_marker


def _get_prereq_info(game_id: str) -> dict[str, Any] | None:
    """Extract the ``prereq_info`` block for one game from Legendary's installed.json.

    Args:
        game_id: Epic Games game identifier.

    Returns:
        A dict combining the prereq info with the game's
        ``install_path``, or ``None`` if the file is missing,
        unreadable, or the game has no prerequisites declared.
    """
    installed_json = _find_legendary_installed_json()
    if installed_json is None:
        logger.debug(
            '[epic_prerequisites] legendary installed.json not found',
        )
        return None
    try:
        with open(installed_json, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            '[epic_prerequisites] installed.json read failed: %s', e,
        )
        return None
    entry = data.get(game_id) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return None
    prereq = entry.get('prereq_info')
    if not isinstance(prereq, dict) or not prereq.get('path'):
        return None
    prereq = dict(prereq)
    prereq['install_path'] = entry.get('install_path', '')
    return prereq


def _find_legendary_installed_json() -> Path | None:
    """Locate Legendary's installed.json across known config locations.

    Checks the standard ``~/.config/legendary/`` first, then the
    Heroic Games Launcher Flatpak override.

    Returns:
        Path to the file, or ``None`` if not found.
    """
    for candidate in _LEGENDARY_CONFIG_CANDIDATES:
        installed = candidate.expanduser() / 'installed.json'
        if installed.is_file():
            return installed
    return None


async def _run_prerequisite(
    plan: ProtonLaunchPlan,
    prereq: dict[str, Any],
    prefix_root: Path,
) -> bool:
    """Execute the prerequisite Windows installer through UMU.

    Builds the env (WINEPREFIX, GAMEID, PROTON_VERB) and the
    command (python + umu-wrapper + installer + args), then
    spawns the installer.

    Args:
        plan: Launch plan (provides env, umu_wrapper, python_bin).
        prereq: Prereq info dict (``path``, ``args``, ``install_path``).
        prefix_root: Canonical prefix root.

    Returns:
        True iff the installer ran to completion (any exit code).
    """
    install_path = prereq.get('install_path') or ''
    rel = prereq.get('path') or ''
    if not install_path or not rel:
        return False
    full_installer = Path(install_path) / rel
    if not full_installer.is_file():
        logger.warning(
            '[epic_prerequisites] installer missing: %s', full_installer,
        )
        return False
    env = _build_prereq_env(plan, prefix_root)
    cmd = _build_prereq_cmd(plan, full_installer, prereq)
    return await _spawn_installer(cmd, env)


def _build_prereq_env(
    plan: ProtonLaunchPlan, prefix_root: Path,
) -> dict[str, str]:
    """Build the environment dict for the prereq subprocess.

    Args:
        plan: Launch plan (provides the base env).
        prefix_root: Canonical prefix root (used for WINEPREFIX).

    Returns:
        Environment dict with WINEPREFIX, GAMEID, PROTON_VERB set
        and ``LD_PRELOAD`` removed.
    """
    env = dict(plan.env)
    env['WINEPREFIX'] = str(prefix_root)
    env['GAMEID'] = 'umu-0'
    env['PROTON_VERB'] = 'waitforexitandrun'
    env.pop('LD_PRELOAD', None)
    return env


def _build_prereq_cmd(
    plan: ProtonLaunchPlan,
    full_installer: Path,
    prereq: dict[str, Any],
) -> list[str]:
    """Build the argv for the prereq installer subprocess.

    Args:
        plan: Launch plan (provides python_bin and umu_wrapper).
        full_installer: Absolute path to the .exe to run.
        prereq: Prereq info dict (may include ``args``).

    Returns:
        Command argv list.
    """
    cmd = [str(plan.python_bin), str(plan.umu_wrapper), str(full_installer)]
    args = prereq.get('args') or ''
    if isinstance(args, str) and args.strip():
        cmd.extend(args.split())
    return cmd


async def _spawn_installer(cmd: list[str], env: dict[str, str]) -> bool:
    """Spawn the prereq installer with a 600s timeout.

    Stdout is captured and filtered for interesting lines
    (error/install/success/...) which are logged at INFO.

    Many Windows installers exit non-zero for 'already
    installed' or partial-success cases, so any termination
    is treated as success and only timeout / spawn errors
    return False — mirrors the legacy bash helper.

    Args:
        cmd: Argv list to execute.
        env: Environment dict.

    Returns:
        True iff the installer didn't time out or fail to spawn.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as e:
        logger.warning(
            '[epic_prerequisites] spawn failed: %s', e,
        )
        return False
    try:
        stdout, _err = await asyncio.wait_for(
            proc.communicate(), timeout=_INSTALLER_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        logger.warning(
            '[epic_prerequisites] installer timed out after %ds',
            _INSTALLER_TIMEOUT_S,
        )
        return False
    if stdout:
        _log_filtered_output(stdout.decode('utf-8', errors='replace'))
    return True


def _log_filtered_output(text: str) -> None:
    """Log only the lines containing recognizable markers.

    Markers: ``error``, ``warn``, ``install``, ``success``,
    ``fail``, ``complete``, ``info:``.

    Args:
        text: Captured stdout (UTF-8 decoded).
    """
    markers = ('error', 'warn', 'install', 'success', 'fail', 'complete', 'info:')
    for line in text.splitlines():
        lower = line.lower()
        if any(m in lower for m in markers):
            logger.info('[epic_prerequisites]   %s', line.strip())


def _write_marker_sync(marker_path: Path, body: str) -> None:
    """Write the prereq-done marker file (best-effort).

    Args:
        marker_path: Marker filename inside the prefix.
        body: Text written to the marker (for human inspection).
    """
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(body, encoding='utf-8')
    except OSError as e:
        logger.debug(
            '[epic_prerequisites] marker write failed: %s', e,
        )


def _cleanup_legacy_marker(legacy_marker: Path) -> None:
    """Best-effort unlink of the legacy (unhashed) prereq marker.

    Args:
        legacy_marker: Path to the old-format marker.
    """
    if not legacy_marker.is_file():
        return
    try:
        os.unlink(legacy_marker)
    except OSError:
        pass
