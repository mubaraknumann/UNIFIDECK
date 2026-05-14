"""Epic registry key injection — writes UplayID-based keys into the Wine registry so Epic titles locate their Ubisoft companion data."""

from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
logger = logging.getLogger(__name__)
_UPLAY_ID_RE = re.compile(r"-UplayId=\s*(\d+)")
@dataclass(frozen=True)
class RegistryInjectionResult:
    """Outcome of an Epic registry injection attempt.

    Attributes:
        success: True iff every ``reg add`` call succeeded.
        keys_written: Number of registry keys actually written.
        reason: Empty on success; one of ``installed_json_missing_or_unreadable``,
            ``no_install_path``, ``wine_binary_not_found``,
            ``partial_reg_add_failures`` on failure.
    """
    success: bool
    keys_written: int
    reason: str = ""
def _normalize_prefix_root(prefix_path: Path) -> Path:
    """Strip trailing ``pfx`` segments to return the canonical prefix root.

    Args:
        prefix_path: Any path inside (or equal to) the Wine prefix.

    Returns:
        The resolved prefix root (the directory CONTAINING ``pfx``).
    """
    p = prefix_path.resolve()
    while p.name == "pfx":
        p = p.parent
    return p
def _select_active_wineprefix(prefix_root: Path) -> Path:
    """Pick the directory that actually holds the Wine registry.

    Tries, in order: the prefix root itself if ``pfx`` is a symlink
    back to it, then ``<root>/pfx/`` if it contains ``system.reg``,
    then the prefix root if it contains ``system.reg``.

    Args:
        prefix_root: Canonical prefix root (from ``_normalize_prefix_root``).

    Returns:
        Path to the directory to point ``WINEPREFIX`` at.
    """
    pfx_path = prefix_root / "pfx"
    try:
        if (
            pfx_path.is_symlink()
            and pfx_path.resolve() == prefix_root.resolve()
        ):
            return prefix_root
    except OSError:
        pass
    if (pfx_path / "system.reg").is_file():
        return pfx_path
    if (prefix_root / "system.reg").is_file():
        return prefix_root
    return pfx_path
def _linux_to_wine_path(linux_path: str) -> str:
    """Convert a Linux absolute path to its Wine equivalent under ``Z:``.

    Args:
        linux_path: Absolute POSIX path.

    Returns:
        Wine path (``Z:\\...``) with a trailing backslash.
    """
    wine_path = "Z:" + linux_path.replace("/", "\\")
    if not wine_path.endswith("\\"):
        wine_path += "\\"
    return wine_path

def _load_installed_json(
    legendary_config: Path,
    game_id: str,
) -> dict | None:

    """Load and validate Legendary's ``installed.json`` for one game.

    Args:
        legendary_config: Path to the Legendary config directory.
        game_id: Epic Games game identifier.

    Returns:
        The per-game dict, or ``None`` if the file is missing,
        unreadable, or doesn't contain the requested game.
    """
    installed_json = legendary_config / "installed.json"
    if not installed_json.is_file():
        logger.error(
            "[epic_registry] installed.json not found at %s",
            installed_json,
        )
        return None
    try:
        with installed_json.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error(
            "[epic_registry] failed to read "
            "installed.json: %s", e,
        )
        return None
    app = data.get(game_id)
    if not app:
        logger.error(
            "[epic_registry] game %s not in "
            "installed.json", game_id,
        )
        return None
    return cast("dict[Any, Any] | None", app)
def _find_wine_binary() -> Path | None:
    """Locate the Wine binary inside the active Proton install.

    Reads ``PROTONPATH`` from the environment and returns
    ``$PROTONPATH/files/bin/wine`` if it exists.

    Returns:
        Path to the Wine binary, or ``None`` if ``PROTONPATH`` is
        unset or the binary is missing.
    """
    proton_path = os.environ.get("PROTONPATH")
    if not proton_path:
        return None
    wine_bin = (
        Path(proton_path) / "files" / "bin" / "wine"
    )
    return wine_bin if wine_bin.is_file() else None

def _build_reg_commands(
    wine_bin: Path,
    game_id: str,
    wine_install_path: str,
    uplay_id: str | None,
) -> list[list[str]]:

    """Build the ``wine reg add`` command lines for one game.

    Always emits three Epic-side keys (AppDataPath +
    Manifests/InstallLocation under WOW6432Node and HKCU).
    When ``uplay_id`` is provided, also emits the Ubisoft
    Launcher ``Installs/<uplay_id>`` keys (InstallDir + Language).

    Args:
        wine_bin: Path to the Wine binary.
        game_id: Epic Games game identifier.
        wine_install_path: Wine-formatted install path.
        uplay_id: Optional Ubisoft Launcher ID (extracted from
            Legendary launch parameters).

    Returns:
        List of argv lists ready to feed ``subprocess`` /
        ``asyncio.create_subprocess_exec``.
    """
    commands: list[list[str]] = [
        [
            str(wine_bin), "reg", "add",
            "HKEY_LOCAL_MACHINE\\Software\\Epic Games\\EpicGamesLauncher",
            "/v", "AppDataPath", "/t", "REG_SZ",
            "/d", "C:\\ProgramData\\Epic\\EpicGamesLauncher\\Data\\",
            "/f",
        ],
        [
            str(wine_bin), "reg", "add",
            (
                "HKEY_LOCAL_MACHINE\\Software\\WOW6432Node\\Epic Games"
                "\\EpicGamesLauncher\\Manifests\\" + game_id
            ),
            "/v", "InstallLocation", "/t", "REG_SZ",
            "/d", wine_install_path, "/f",
        ],
        [
            str(wine_bin), "reg", "add",
            (
                "HKEY_CURRENT_USER\\Software\\Epic Games"
                "\\EpicGamesLauncher\\Manifests\\" + game_id
            ),
            "/v", "InstallLocation", "/t", "REG_SZ",
            "/d", wine_install_path, "/f",
        ],
    ]
    if uplay_id:
        commands.extend([
            [
                str(wine_bin), "reg", "add",
                (
                    "HKEY_LOCAL_MACHINE\\Software\\WOW6432Node\\Ubisoft"
                    "\\Launcher\\Installs\\" + uplay_id
                ),
                "/v", "InstallDir", "/t", "REG_SZ",
                "/d", wine_install_path, "/f",
            ],
            [
                str(wine_bin), "reg", "add",
                (
                    "HKEY_LOCAL_MACHINE\\Software\\WOW6432Node\\Ubisoft"
                    "\\Launcher\\Installs\\" + uplay_id
                ),
                "/v", "Language", "/t", "REG_SZ",
                "/d", "en-US", "/f",
            ],
        ])
    return commands

async def _run_reg_commands(
    commands: list[list[str]],
    env: dict,
) -> int:

    """Run each ``reg add`` command sequentially with a 30s timeout.

    Failures and timeouts are logged but do not abort the loop.

    Args:
        commands: argv lists from ``_build_reg_commands``.
        env: Environment dict (must include ``WINEPREFIX``).

    Returns:
        Number of commands that exited with code 0.
    """
    ok_count = 0
    for cmd in commands:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=30,
                )
            except TimeoutError:
                logger.error(
                    "[epic_registry] reg add timed out: %s",
                    cmd[3],
                )
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                continue
            if proc.returncode == 0:
                ok_count += 1
            else:
                logger.error(
                    "[epic_registry] reg add failed: %s: %s",
                    cmd[3],
                    stderr.decode(errors="replace").strip(),
                )
        except (OSError, subprocess.SubprocessError) as e:
            logger.error(
                "[epic_registry] reg add spawn error: %s", e,
            )
            continue
    return ok_count
async def _kill_wineserver(
    wine_bin: Path, wineprefix: Path,
) -> None:
    """Best-effort ``wineserver --kill`` to release the prefix lock.

    Called after the reg-add pass so subsequent Proton launches
    see the new registry values. Failures are silent.

    Args:
        wine_bin: Path to the Wine binary (used to find ``wineserver``).
        wineprefix: Path to set as ``WINEPREFIX`` for the kill call.
    """
    wineserver = wine_bin.parent / "wineserver"
    if not wineserver.is_file():
        return
    env = dict(os.environ)
    env["WINEPREFIX"] = str(wineprefix)
    try:
        proc = await asyncio.create_subprocess_exec(
            str(wineserver), "--kill",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
        logger.info(
            "[epic_registry] killed stale wineserver "
            "after setup",
        )
    except (
        TimeoutError, OSError,
        subprocess.SubprocessError,
    ):
        pass
def _resolve_install_paths(
    app: dict[str, Any],
) -> tuple[str, str | None] | None:
    """Extract the Wine install path and Uplay ID from a Legendary app entry.

    Args:
        app: One entry from ``installed.json``.

    Returns:
        Tuple ``(wine_install_path, uplay_id)`` (uplay_id is
        ``None`` if no ``-UplayId=`` flag is present in
        ``launch_parameters``), or ``None`` if ``install_path``
        is missing.
    """
    install_path = app.get("install_path")
    if not install_path:
        return None
    wine_install_path = _linux_to_wine_path(install_path)
    launch_params = app.get("launch_parameters", "") or ""
    uplay_match = _UPLAY_ID_RE.search(launch_params)
    uplay_id = uplay_match.group(1) if uplay_match else None
    return wine_install_path, uplay_id

def _error_result(reason: str) -> RegistryInjectionResult:

    """Shorthand to build a failed ``RegistryInjectionResult``.

    Args:
        reason: Stable error code (e.g. ``"wine_binary_not_found"``).

    Returns:
        ``RegistryInjectionResult`` with ``success=False`` and
        ``keys_written=0``.
    """
    return RegistryInjectionResult(
        success=False, keys_written=0, reason=reason,
    )
async def setup_registry(
    game_id: str,
    prefix_path: Path,
    legendary_config: Path,
) -> RegistryInjectionResult:
    """Inject the Epic registry keys for one installed game.

    Pipeline: normalize prefix root → load ``installed.json`` →
    resolve Wine install path and optional Uplay ID → locate
    Wine binary → select the active wineprefix → build and run
    the ``reg add`` commands → kill the wineserver to flush.

    Args:
        game_id: Epic Games game identifier.
        prefix_path: Path to the Wine prefix (any subpath).
        legendary_config: Legendary config directory (holds
            ``installed.json``).

    Returns:
        ``RegistryInjectionResult`` summarizing the operation.
    """
    prefix_root = _normalize_prefix_root(prefix_path)
    app = _load_installed_json(legendary_config, game_id)
    if app is None:
        return _error_result("installed_json_missing_or_unreadable")
    paths = _resolve_install_paths(app)
    if paths is None:
        logger.error(
            "[epic_registry] no install_path for %s", game_id,
        )
        return _error_result("no_install_path")
    wine_install_path, uplay_id = paths
    wine_bin = _find_wine_binary()
    if wine_bin is None:
        logger.error(
            "[epic_registry] PROTONPATH not set or wine "
            "binary missing",
        )
        return _error_result("wine_binary_not_found")
    active_prefix = _select_active_wineprefix(prefix_root)
    env = dict(os.environ)
    env["WINEPREFIX"] = str(active_prefix)
    commands = _build_reg_commands(
        wine_bin=wine_bin,
        game_id=game_id,
        wine_install_path=wine_install_path,
        uplay_id=uplay_id,
    )
    ok_count = await _run_reg_commands(commands, env)
    await _kill_wineserver(wine_bin, active_prefix)
    total = len(commands)
    all_ok = ok_count == total
    logger.info(
        "[epic_registry] setup for %s (uplay=%s): %d/%d keys",
        game_id, uplay_id, ok_count, total,
    )
    return RegistryInjectionResult(
        success=all_ok,
        keys_written=ok_count,
        reason="" if all_ok else "partial_reg_add_failures",
    )