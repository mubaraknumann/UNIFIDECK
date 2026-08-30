"""GameVault archive handling — detect the format, unpack it.

Split out of ``install.py`` to keep that file under the 550-LOC volumetry
cap. The GameVault server hands back whatever archive the user put in their
library, so the format is discovered from magic bytes rather than the
filename, and unpacking rar/7z shells out to whichever tool the host has.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

_ARCH_ZIP = "zip"
_ARCH_RAR = "rar"
_ARCH_7Z = "7z"


def _mkdir_p(path: Path) -> None:
    """``mkdir -p``, as a named function so it can go to a thread."""
    path.mkdir(parents=True, exist_ok=True)


def _detect_format(path: Path) -> str | None:
    """Detect archive format from magic bytes."""
    try:
        with path.open("rb") as fh:
            header = fh.read(8)
    except Exception:
        return None

    if header[:2] == b"PK":
        return _ARCH_ZIP
    if header[:3] == b"Rar":
        return _ARCH_RAR
    if header[:6] == b"7z\xbc\xaf'\x1c":
        return _ARCH_7Z
    # 7z stored inside SFX: scan first 512 KB
    try:
        with path.open("rb") as fh:
            chunk = fh.read(512 * 1024)
        sig = b"7z\xbc\xaf'\x1c"
        if sig in chunk:
            return _ARCH_7Z
    except OSError as exc:
        logger.warning(
            "[GameVaultInstaller] could not scan %s for an SFX signature: %s",
            path.name, exc,
        )
    return None


async def _extract_archive(archive: Path, dest: Path) -> None:
    """Unpack *archive* into *dest*, dispatching on its magic bytes.

    Split out of ``install_game``, which was over the 10-call fan-out cap:
    detection, the destination mkdir and the three per-format extractors are
    one step of that pipeline, not five. Raises on an unknown or unsupported
    format so the caller's single failure path reports it, rather than each
    branch building its own ``InstallResult``.
    """
    fmt = _detect_format(archive)
    if fmt is None:
        raise RuntimeError(f"Unknown archive format: {archive.name}")
    await asyncio.to_thread(_mkdir_p, dest)
    if fmt == _ARCH_ZIP:
        await asyncio.to_thread(_extract_zip, archive, dest)
    elif fmt == _ARCH_RAR:
        await _extract_rar(archive, dest)
    elif fmt == _ARCH_7Z:
        await _extract_with_7z(archive, dest)
    else:
        raise RuntimeError(f"Unsupported archive format: {fmt}")


def _extract_zip(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive, "r") as zf:
        zf.extractall(str(dest))


async def _extract_rar(archive: Path, dest: Path) -> None:
    """Try bsdtar → unrar → 7z for RAR extraction."""
    for tool in ("bsdtar", "unrar", "7z"):
        if shutil.which(tool):
            if tool == "bsdtar":
                cmd = ["bsdtar", "-xf", str(archive), "-C", str(dest)]
            elif tool == "unrar":
                cmd = ["unrar", "x", "-y", str(archive), str(dest) + "/"]
            else:
                cmd = ["7z", "x", str(archive), f"-o{dest}", "-y"]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0:
                return
            logger.warning(
                "[GameVaultInstaller] %s failed: %s",
                tool,
                stderr.decode(errors="replace")[:200],
            )

    raise RuntimeError("No RAR extraction tool available (bsdtar/unrar/7z)")


async def _extract_with_7z(archive: Path, dest: Path) -> None:
    if not shutil.which("7z"):
        raise RuntimeError("7z binary not found")
    cmd = ["7z", "x", str(archive), f"-o{dest}", "-y"]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"7z extraction failed: {stderr.decode(errors='replace')[:200]}"
        )
