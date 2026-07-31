"""GameVault install pipeline — download archive, extract, find exe.

Design note — separate download_dir / install_dir:
    GameVault archives can be very large.  If the user wants to install
    to an SSD with limited free space the archive download itself might
    not fit alongside the fully-extracted game.  We therefore support a
    *separate* temporary download directory (``download_dir``) for the
    archive file.  After extraction the archive is deleted so only the
    final extracted game remains in ``install_path``.

    Pipeline:
        1. HEAD  /api/games/{id}/download  → resolve filename + size
        2. GET   /api/games/{id}/download  → stream to ``download_dir``
        3. Detect archive format from magic bytes
        4. Extract to ``install_path/{title}/``
        5. Delete archive from ``download_dir``
        6. Score .exe files, persist install marker JSON
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import struct
import zipfile
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from unifideck.core.types import InstallResult, Result

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]

_MARKER_DIR = Path("~/.local/share/unifideck/gamevault_installed").expanduser()

_ARCH_ZIP = "zip"
_ARCH_RAR = "rar"
_ARCH_7Z = "7z"

# Exe scoring heuristics
_UTIL_KEYWORDS = (
    "uninstall", "setup", "install", "redist", "vcredist",
    "directx", "dxsetup", "ue4", "ue5", "crash", "report",
    "_commonredist", "support", "dotnet",
)
_GOOD_DEPTH = 3   # prefer shallow paths


class GameVaultInstaller:
    """Download → extract → register GameVault games."""

    def __init__(
        self,
        *,
        default_install_root: str,
        download_dir: str,
    ) -> None:
        self._default_install_root = Path(default_install_root).expanduser()
        self._download_dir = Path(download_dir).expanduser()

    # ── Public API ──────────────────────────────────────────────────

    async def install_game(
        self,
        game_id: str,
        *,
        auth_headers: dict[str, str],
        server_url: str,
        verify_ssl: bool,
        install_path: str | None,
        progress_callback: ProgressCallback | None = None,
        download_dir: str | None = None,
    ) -> InstallResult:
        """Download and extract a GameVault game."""
        target_dir = (
            Path(install_path).expanduser()
            if install_path
            else self._default_install_root
        )
        # Per-install override takes precedence over the configured default.
        effective_dl_dir = Path(download_dir).expanduser() if download_dir else self._download_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        effective_dl_dir.mkdir(parents=True, exist_ok=True)
        _MARKER_DIR.mkdir(parents=True, exist_ok=True)

        archive_path: Path | None = None
        try:
            # Step 1 — download archive to temp dir
            archive_path = await self._download_archive(
                game_id=game_id,
                server_url=server_url,
                auth_headers=auth_headers,
                verify_ssl=verify_ssl,
                progress_callback=progress_callback,
                download_dir=effective_dl_dir,
            )

            # Step 2 — detect format and extract to target_dir/title/
            fmt = _detect_format(archive_path)
            if fmt is None:
                return InstallResult(
                    success=False,
                    error="Unknown archive format",
                    store="gamevault",
                    game_id=game_id,
                )

            if progress_callback:
                await progress_callback({"phase": "extracting"})

            game_dir = target_dir / archive_path.stem
            game_dir.mkdir(parents=True, exist_ok=True)

            if fmt == _ARCH_ZIP:
                await asyncio.to_thread(_extract_zip, archive_path, game_dir)
            elif fmt == _ARCH_RAR:
                await _extract_rar(archive_path, game_dir)
            elif fmt == _ARCH_7Z:
                await _extract_with_7z(archive_path, game_dir)
            else:
                return InstallResult(
                    success=False,
                    error=f"Unsupported archive format: {fmt}",
                    store="gamevault",
                    game_id=game_id,
                )

            # Step 3 — find executable
            exe_path = _find_executable(str(game_dir))

            # Step 4 — write install marker
            title = archive_path.stem
            _save_install_info(
                game_id,
                title=title,
                install_path=str(game_dir),
                exe_path=exe_path or "",
            )

            return InstallResult(
                success=True,
                store="gamevault",
                game_id=game_id,
                install_path=str(game_dir),
                metadata={"exe_path": exe_path or "", "title": title},
            )

        except Exception as exc:  # noqa: BLE001
            logger.error("[GameVaultInstaller] install_game failed: %s", exc)
            return InstallResult(
                success=False,
                error=str(exc),
                store="gamevault",
                game_id=game_id,
            )
        finally:
            # Always remove the archive from the temp download dir
            if archive_path and archive_path.exists():
                try:
                    archive_path.unlink()
                    logger.info("[GameVaultInstaller] Removed archive %s", archive_path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[GameVaultInstaller] Could not delete archive %s: %s",
                        archive_path,
                        exc,
                    )

    async def uninstall_game(self, game_id: str) -> Result:
        """Remove game files and install marker."""
        info = self.get_install_info(game_id)
        if not info:
            return Result(
                success=False,
                error="Game not installed",
                store="gamevault",
            )
        install_path = Path(info.get("install_path", ""))
        if install_path.exists():
            try:
                await asyncio.to_thread(shutil.rmtree, str(install_path), True)
                logger.info(
                    "[GameVaultInstaller] Removed install dir %s", install_path
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[GameVaultInstaller] Could not remove %s: %s", install_path, exc
                )
        _remove_install_info(game_id)
        return Result(success=True, store="gamevault")

    async def get_game_size(
        self,
        game_id: str,
        *,
        auth_headers: dict[str, str],
        server_url: str,
        verify_ssl: bool,
    ) -> int | None:
        """HEAD /api/games/{id}/download → Content-Length."""
        url = f"{server_url}/api/games/{game_id}/download"
        try:
            connector = aiohttp.TCPConnector(ssl=verify_ssl)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.head(
                    url,
                    headers=auth_headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                    allow_redirects=True,
                ) as resp:
                    cl = resp.headers.get("Content-Length", "")
                    if cl.isdigit():
                        return int(cl)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[GameVaultInstaller] get_game_size error: %s", exc)
        return None

    # ── Marker helpers (called by store.py) ────────────────────────

    def get_install_info(self, game_id: str) -> dict[str, Any] | None:
        return _load_install_info(game_id)

    def get_installed(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        if not _MARKER_DIR.exists():
            return result
        for f in _MARKER_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                gid = f.stem
                result[gid] = data
            except Exception:  # noqa: BLE001
                pass
        return result

    # ── Download helper ─────────────────────────────────────────────

    async def _download_archive(
        self,
        *,
        game_id: str,
        server_url: str,
        auth_headers: dict[str, str],
        verify_ssl: bool,
        progress_callback: ProgressCallback | None,
        download_dir: Path,
    ) -> Path:
        url = f"{server_url}/api/games/{game_id}/download"
        connector = aiohttp.TCPConnector(ssl=verify_ssl)

        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                url,
                headers=auth_headers,
                timeout=aiohttp.ClientTimeout(total=0),  # no overall timeout
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(
                        f"Download returned HTTP {resp.status}"
                    )

                # Resolve filename from Content-Disposition header
                cd = resp.headers.get("Content-Disposition", "")
                filename = _parse_filename_from_cd(cd) or f"gamevault_{game_id}.bin"
                archive_path = download_dir / filename

                # Content-Length may be absent for chunked transfers; fall back to 0
                # which yields pct=0 instead of crashing.
                total = int(resp.headers.get("Content-Length") or resp.content_length or 0)
                downloaded = 0
                last_report = 0.0
                import time

                start_time = time.monotonic()
                with archive_path.open("wb") as fh:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        fh.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if progress_callback and now - last_report >= 1.0:
                            pct = (downloaded / total * 100) if total else 0
                            elapsed = now - start_time
                            speed_bps = downloaded / elapsed if elapsed > 0 else 0
                            remaining = total - downloaded
                            eta = int(remaining / speed_bps) if speed_bps > 0 and total > 0 else 0
                            await progress_callback(
                                {
                                    "phase": "downloading",
                                    "percentage": round(pct, 1),
                                    "downloaded_bytes": downloaded,
                                    "total_bytes": total,
                                    "speed_bps": speed_bps,
                                    "eta_seconds": eta,
                                }
                            )
                            last_report = now

        logger.info(
            "[GameVaultInstaller] Downloaded %s (%d bytes) to %s",
            filename,
            downloaded,
            download_dir,
        )
        return archive_path


# ── Archive detection & extraction ─────────────────────────────────────────

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
    except Exception:
        pass
    return None


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


# ── Exe finder ──────────────────────────────────────────────────────────────

def _find_executable(install_dir: str) -> str | None:
    """Score .exe candidates and return the best match."""
    candidates: list[tuple[float, str]] = []

    for dirpath, _dirs, files in os.walk(install_dir):
        depth = len(Path(dirpath).relative_to(install_dir).parts)
        for fname in files:
            if not fname.lower().endswith(".exe"):
                continue
            lower = fname.lower()
            # Penalise utility executables
            if any(k in lower for k in _UTIL_KEYWORDS):
                continue
            full = os.path.join(dirpath, fname)
            # Score: prefer shallow + larger file
            size = 0
            try:
                size = os.path.getsize(full)
            except OSError:
                pass
            depth_score = max(0, _GOOD_DEPTH - depth)
            size_score = min(size / (100 * 1024 * 1024), 5.0)  # cap at 5 pts
            score = depth_score + size_score
            candidates.append((score, full))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


# ── Install marker helpers ───────────────────────────────────────────────────

def _marker_path(game_id: str) -> Path:
    return _MARKER_DIR / f"{game_id}.json"


def _save_install_info(
    game_id: str,
    *,
    title: str,
    install_path: str,
    exe_path: str,
) -> None:
    try:
        _MARKER_DIR.mkdir(parents=True, exist_ok=True)
        _marker_path(game_id).write_text(
            json.dumps(
                {
                    "game_id": game_id,
                    "title": title,
                    "install_path": install_path,
                    "exe_path": exe_path,
                },
                indent=2,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("[GameVaultInstaller] Could not save marker: %s", exc)


def _load_install_info(game_id: str) -> dict[str, Any] | None:
    p = _marker_path(game_id)
    try:
        if p.exists():
            return json.loads(p.read_text())
    except Exception as exc:  # noqa: BLE001
        logger.debug("[GameVaultInstaller] Could not read marker: %s", exc)
    return None


def _remove_install_info(game_id: str) -> None:
    p = _marker_path(game_id)
    try:
        if p.exists():
            p.unlink()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[GameVaultInstaller] Could not remove marker: %s", exc)


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _parse_filename_from_cd(content_disposition: str) -> str | None:
    """Extract filename from Content-Disposition header."""
    import re

    m = re.search(r'filename\*?=["\']?([^"\';\r\n]+)["\']?', content_disposition)
    if m:
        name = m.group(1).strip()
        # Strip RFC 5987 charset prefix, e.g. "UTF-8''Filename.zip"
        if "''" in name:
            name = name.split("''", 1)[1]
        return name.strip('"\'')
    return None
