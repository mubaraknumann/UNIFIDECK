"""Epic-prefix repair routines — resolves Wine binary location and applies the Epic Games Launcher compatibility tweaks."""

from __future__ import annotations
import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path
logger = logging.getLogger(__name__)
_PROTON_FALLBACK_WINE_PATHS: list[str] = [
    "~/.steam/steam/steamapps/common/Proton - Experimental/files/bin/wine",
    "~/.steam/steam/steamapps/common/Proton 10.0/files/bin/wine",
    "~/.steam/steam/steamapps/common/Proton 9.0 (Beta)/files/bin/wine",
]
def normalize_prefix_root(prefix_path: Path) -> Path:
    """Strip trailing ``pfx`` segments to return the canonical prefix root.

    Args:
        prefix_path: Any path inside (or equal to) the Wine prefix.

    Returns:
        The resolved prefix root.
    """
    p = prefix_path.resolve()
    while p.name == "pfx":
        p = p.parent
    return p

def _copy_wrapper_to_drive_c(
    drive_c: Path,
    bundled_wrapper: Path,
    label: str,
) -> bool:

    """Drop the EpicGamesLauncher.exe wrapper into the Wine drive_c.

    Copies the bundled stub to both ``Program Files (x86)/Epic
    Games/Launcher/Portal/Binaries/Win32/`` and
    ``windows/command/`` (Legendary sets
    ``LEGENDARY_WRAPPER_EXE`` to the latter as a fallback).

    Args:
        drive_c: Wine ``drive_c`` directory inside the prefix.
        bundled_wrapper: Source wrapper binary shipped with the plugin.
        label: Free-form label for logs (``"root"`` / ``"pfx"``).

    Returns:
        True iff at least one of the two destinations was written.
    """
    if not bundled_wrapper.is_file():
        logger.warning(
            "[epic_prefix_fix] bundled wrapper missing at %s",
            bundled_wrapper,
        )
        return False
    copied = False
    epic_dir = (
        drive_c / "Program Files (x86)" / "Epic Games" / "Launcher"
        / "Portal" / "Binaries" / "Win32"
    )
    try:
        epic_dir.mkdir(parents=True, exist_ok=True)
        epic_target = epic_dir / "EpicGamesLauncher.exe"
        if epic_target.exists():
            try:
                epic_target.unlink()
            except OSError:
                pass
        shutil.copy2(bundled_wrapper, epic_target)
        logger.info(
            "[epic_prefix_fix] copied wrapper to Epic dir (%s)",
            label,
        )
        copied = True
    except OSError as e:
        logger.warning(
            "[epic_prefix_fix] failed to copy to Epic dir "
            "(%s): %s",
            label, e,
        )
    win_command_dir = drive_c / "windows" / "command"
    try:
        win_command_dir.mkdir(parents=True, exist_ok=True)
        win_target = win_command_dir / "EpicGamesLauncher.exe"
        if win_target.exists():
            try:
                win_target.unlink()
            except OSError:
                pass
        shutil.copy2(bundled_wrapper, win_target)
        logger.info(
            "[epic_prefix_fix] copied wrapper to "
            "windows/command (%s)",
            label,
        )
        copied = True
    except OSError as e:
        logger.warning(
            "[epic_prefix_fix] failed to copy to "
            "windows/command (%s): %s",
            label, e,
        )
    return copied
def _find_wine_binary() -> Path | None:
    """Locate a usable Wine binary.

    Tries, in order: ``$PROTONPATH/files/bin/wine``, several
    well-known Proton install paths under ``~/.steam/``, then
    the system ``wine`` via ``shutil.which``.

    Returns:
        Path to the binary or ``None`` if nothing was found.
    """
    proton_env = os.environ.get("PROTONPATH")
    if proton_env:
        candidate = Path(proton_env) / "files" / "bin" / "wine"
        if candidate.is_file():
            return candidate
    for fallback in _PROTON_FALLBACK_WINE_PATHS:
        candidate = Path(fallback).expanduser()
        if candidate.is_file():
            return candidate
    system_wine = shutil.which("wine")
    if system_wine:
        return Path(system_wine)
    return None

def _select_registry_prefix(
    prefix_root: Path,
) -> Path:

    """Pick the directory to point ``WINEPREFIX`` at for reg-add calls.

    If ``pfx`` is a symlink back to the prefix root → root.
    Else if ``pfx/drive_c`` exists → ``pfx``.
    Else → root.

    Args:
        prefix_root: Canonical prefix root.

    Returns:
        Path suitable for WINEPREFIX.
    """
    pfx_candidate = prefix_root / "pfx"
    if not pfx_candidate.exists():
        return prefix_root
    try:
        if pfx_candidate.is_symlink() and pfx_candidate.resolve() == prefix_root.resolve():
            return prefix_root
    except OSError:
        pass
    drive_c = pfx_candidate / "drive_c"
    if drive_c.is_dir():
        return pfx_candidate
    return prefix_root
async def _run_registry_inject(
    wine_bin: Path,
    wineprefix: Path,
) -> bool:
    """Add the Epic-launcher URL-scheme registry key (30s timeout).

    Args:
        wine_bin: Path to the Wine binary.
        wineprefix: Path to set as WINEPREFIX for the call.

    Returns:
        True iff the ``wine reg add`` call exited 0.
    """
    env = dict(os.environ)
    env["WINEPREFIX"] = str(wineprefix)
    try:
        proc = await asyncio.create_subprocess_exec(
            str(wine_bin),
            "reg", "add",
            "HKEY_CLASSES_ROOT\\\\com.epicgames.launcher",
            "/f",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=30)
        except TimeoutError:
            logger.warning(
                "[epic_prefix_fix] wine reg add timed out, "
                "killing",
            )
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return False
        if rc == 0:
            logger.info(
                "[epic_prefix_fix] registry key injected",
            )
            return True
        logger.warning(
            "[epic_prefix_fix] wine reg add exited rc=%d", rc,
        )
        return False
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(
            "[epic_prefix_fix] registry injection failed: %s", e,
        )
        return False
async def _kill_wineserver(wine_bin: Path, wineprefix: Path) -> None:
    """Best-effort ``wineserver --kill`` to flush the prefix.

    Args:
        wine_bin: Path to the Wine binary (used to find ``wineserver``).
        wineprefix: Path passed as WINEPREFIX to the kill call.
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
            "[epic_prefix_fix] killed stale wineserver",
        )
    except (TimeoutError, OSError, subprocess.SubprocessError):
        pass

async def apply_epic_launcher_fix(
    prefix_path: Path,
    bundled_wrapper: Path,
) -> bool:

    """Install the Epic-launcher wrapper and seed the registry key.

    Steps: normalize prefix → copy the wrapper into both
    drive_c locations (root and ``pfx``) → locate Wine →
    select the active prefix → inject the registry key →
    kill wineserver.

    Args:
        prefix_path: Path to the Wine prefix (any subpath).
        bundled_wrapper: Bundled wrapper binary to install.

    Returns:
        True if the wrapper was copied (registry injection is
        best-effort and does not affect the return value).
    """
    prefix_root = normalize_prefix_root(prefix_path)
    root_drive_c = prefix_root / "drive_c"
    pfx_drive_c = prefix_root / "pfx" / "drive_c"
    found_any = False
    if root_drive_c.is_dir():
        _copy_wrapper_to_drive_c(root_drive_c, bundled_wrapper, "root")
        found_any = True
        if pfx_drive_c.is_dir():
            _copy_wrapper_to_drive_c(
                pfx_drive_c, bundled_wrapper, "pfx",
            )
            found_any = True
    if not found_any:
        logger.info(
            "[epic_prefix_fix] prefix not initialized yet, "
            "skipping",
        )
        return False
    wine_bin = _find_wine_binary()
    if wine_bin is None:
        logger.warning(
            "[epic_prefix_fix] no wine binary found, "
            "skipping registry injection",
        )
        return True
    registry_prefix = _select_registry_prefix(prefix_root)
    registry_ok = await _run_registry_inject(
        wine_bin, registry_prefix,
    )
    await _kill_wineserver(wine_bin, registry_prefix)
    if registry_ok:
        logger.info("[epic_prefix_fix] quick fix complete")
    else:
        logger.warning(
            "[epic_prefix_fix] quick fix completed with "
            "registry issues (non-fatal)",
        )
    return True