"""GameVault archive handling — detect the format, unpack it.

The archive is whatever its owner put in the library, so the format is
discovered from magic bytes rather than the filename, and unpacking anything
but a zip shells out to whichever tool the host has.

**One ladder for every non-zip format, ``bsdtar`` first.** This used to be two
ladders: rar tried ``bsdtar`` → ``unrar`` → ``7z``, while 7z required the
``7z`` binary and nothing else. Stock SteamOS does not ship ``7z`` — it ships
``bsdtar``, whose libarchive reads 7z, rar, iso and cab — so a ``.7z`` upload
failed on an untouched Deck *after* the user had waited out a multi-gigabyte
download. The two ladders had no reason to differ; the difference was just
where each was written.
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

# Ordered by preference. ``bsdtar`` is in the SteamOS base image and handles
# every format below; the other two are fallbacks for hosts where libarchive
# was built without a codec, and ``unrar`` for rar specifically.
_EXTRACTORS: tuple[str, ...] = ("bsdtar", "7z", "unrar")

_SFX_SCAN_BYTES = 512 * 1024


def mkdir_p(path: Path) -> None:
    """``mkdir -p``, as a named function so it can go to a thread."""
    path.mkdir(parents=True, exist_ok=True)


def available_extractors() -> tuple[str, ...]:
    """Which of :data:`_EXTRACTORS` are on PATH, in preference order.

    Used at connect time to tell the user up front that a format is
    unreachable on this device, rather than after a download or at the end of
    a long extract.
    """
    return tuple(tool for tool in _EXTRACTORS if shutil.which(tool))


def detect_format(path: Path) -> str | None:
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
    # 7z stored inside an SFX stub: scan the first 512 KB.
    try:
        with path.open("rb") as fh:
            chunk = fh.read(_SFX_SCAN_BYTES)
        if b"7z\xbc\xaf'\x1c" in chunk:
            return _ARCH_7Z
    except OSError as exc:
        logger.warning(
            "[GameVault archive] could not scan %s for an SFX signature: %s",
            path.name, exc,
        )
    return None


async def extract_archive(archive: Path, dest: Path) -> None:
    """Unpack *archive* into *dest*, dispatching on its magic bytes.

    Raises on an unknown or unsupported format so the caller's single failure
    path reports it, rather than each branch building its own
    ``InstallResult``.
    """
    fmt = detect_format(archive)
    if fmt is None:
        raise RuntimeError(f"Unknown archive format: {archive.name}")
    await asyncio.to_thread(mkdir_p, dest)
    if fmt == _ARCH_ZIP:
        await asyncio.to_thread(_extract_zip, archive, dest)
        return
    await _extract_with_tool(archive, dest, fmt)


def _extract_zip(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive, "r") as zf:
        zf.extractall(str(dest))


def _command_for(tool: str, archive: Path, dest: Path) -> list[str]:
    if tool == "bsdtar":
        return ["bsdtar", "-xf", str(archive), "-C", str(dest)]
    if tool == "unrar":
        return ["unrar", "x", "-y", str(archive), str(dest) + "/"]
    return ["7z", "x", str(archive), f"-o{dest}", "-y"]


async def _extract_with_tool(archive: Path, dest: Path, fmt: str) -> None:
    """Try each available extractor in turn until one succeeds."""
    tried: list[str] = []
    for tool in _EXTRACTORS:
        if tool == "unrar" and fmt != _ARCH_RAR:
            continue
        if not shutil.which(tool):
            continue
        tried.append(tool)
        proc = await asyncio.create_subprocess_exec(
            *_command_for(tool, archive, dest),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            return
        logger.warning(
            "[GameVault archive] %s failed on %s: %s",
            tool, archive.name, stderr.decode(errors="replace")[:200],
        )

    if not tried:
        raise RuntimeError(
            f"No tool available to extract {fmt} "
            f"(looked for: {', '.join(_EXTRACTORS)})",
        )
    raise RuntimeError(
        f"Could not extract {archive.name} ({fmt}); tried {', '.join(tried)}",
    )
