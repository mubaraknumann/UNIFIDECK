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
        3. Extract to ``install_path/{title}/`` (format detection and the
           per-format extractors live in ``archive.py``)
        4. Delete archive from ``download_dir``
        5. Score .exe files, persist install marker JSON
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp

from unifideck.core.types import InstallResult, Result

from .archive import _extract_archive, _mkdir_p

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]

_MARKER_DIR = Path("~/.local/share/unifideck/gamevault_installed").expanduser()

# Exe scoring heuristics
# Substrings that mark an executable as a bundled utility rather than the
# game. ``unins`` rather than ``uninstall``: Inno Setup, which most of these
# archives are built with, names its uninstaller ``unins000.exe`` — a name
# that contains none of the longer words and was scoring as the game
# whenever the filesystem happened to walk it first.
_UTIL_KEYWORDS = (
    "unins", "uninstall", "setup", "install", "redist", "vcredist",
    "directx", "dxsetup", "ue4", "ue5", "crash", "report",
    "_commonredist", "support", "dotnet",
)
_GOOD_DEPTH = 3   # prefer shallow paths
_CHUNK_BYTES = 1024 * 1024
_REPORT_INTERVAL_S = 1.0


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

    async def _prepare_dirs(
        self,
        install_path: str | None,
        download_dir: str | None,
    ) -> tuple[Path, Path]:
        """Resolve and create the install and archive directories.

        Split out of :meth:`install_game` to keep it under the line cap.
        Returns ``(target_dir, effective_download_dir)``.
        """
        # expanduser() reads $HOME and touches no filesystem, so it is not
        # the blocking call ASYNC240 is looking for; the mkdir below is, and
        # that one goes to a thread.
        target_dir = (
            Path(install_path).expanduser()  # noqa: ASYNC240
            if install_path
            else self._default_install_root
        )
        # Per-install override takes precedence over the configured default.
        effective_dl_dir = (
            Path(download_dir).expanduser()  # noqa: ASYNC240
            if download_dir
            else self._download_dir
        )
        # Off-thread: these can touch a sleeping SD card or a network mount,
        # and this coroutine shares the event loop with the download queue.
        for d in (target_dir, effective_dl_dir, _MARKER_DIR):
            await asyncio.to_thread(_mkdir_p, d)
        return target_dir, effective_dl_dir

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
        target_dir, effective_dl_dir = await self._prepare_dirs(
            install_path, download_dir,
        )

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
            if progress_callback:
                await progress_callback({"phase": "extracting"})
            game_dir = target_dir / archive_path.stem
            await _extract_archive(archive_path, game_dir)

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

        except Exception as exc:
            logger.exception("[GameVaultInstaller] install_game failed")
            return InstallResult(
                success=False,
                error=str(exc),
                store="gamevault",
                game_id=game_id,
            )
        finally:
            _discard_archive(archive_path)

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
        if await asyncio.to_thread(install_path.exists):
            try:
                await asyncio.to_thread(shutil.rmtree, str(install_path), True)
                logger.info(
                    "[GameVaultInstaller] Removed install dir %s", install_path
                )
            except Exception:
                logger.exception(
                    "[GameVaultInstaller] Could not remove %s", install_path,
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
        except Exception as exc:
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
                result[f.stem] = json.loads(f.read_text())
            except (OSError, ValueError) as exc:
                # One unreadable marker must not hide every other
                # installed game, but it should be visible: this is how a
                # game silently reads as not-installed.
                logger.warning(
                    "[GameVaultInstaller] skipping unreadable marker %s: %s",
                    f.name, exc,
                )
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
        """Stream the game archive to *download_dir*, reporting progress."""
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
                archive_path = download_dir / _safe_archive_name(
                    _parse_filename_from_cd(
                        resp.headers.get("Content-Disposition", ""),
                    ),
                    game_id,
                )
                # Content-Length may be absent for chunked transfers; fall
                # back to 0, which yields pct=0 instead of crashing.
                total = int(
                    resp.headers.get("Content-Length")
                    or resp.content_length
                    or 0,
                )
                downloaded = await _stream_to_file(
                    resp.content, archive_path, total, progress_callback,
                )

        logger.info(
            "[GameVaultInstaller] Downloaded %s (%d bytes) to %s",
            archive_path.name,
            downloaded,
            download_dir,
        )
        return archive_path


async def _stream_to_file(
    content: Any,
    archive_path: Path,
    total: int,
    progress_callback: ProgressCallback | None,
) -> int:
    """Write *content* to *archive_path*, reporting at most once a second.

    Returns the number of bytes written. Split out of ``_download_archive``
    because that function was over both the 15-locals and 4-nesting caps —
    the loop, its rate limiter and its rate arithmetic are one concern and
    they are the whole overage.
    """
    downloaded = 0
    last_report = 0.0
    start = time.monotonic()
    with archive_path.open("wb") as fh:
        async for chunk in content.iter_chunked(_CHUNK_BYTES):
            fh.write(chunk)
            downloaded += len(chunk)
            now = time.monotonic()
            if progress_callback and now - last_report >= _REPORT_INTERVAL_S:
                await progress_callback(
                    _progress_payload(downloaded, total, now - start),
                )
                last_report = now
    return downloaded


def _progress_payload(
    downloaded: int, total: int, elapsed: float,
) -> dict[str, Any]:
    """One ``DOWNLOAD_PROGRESS`` tick, in the worker's dict shape.

    ``phase`` only — no ``phase_message``: the UI localises from the phase,
    and the decorative message producers were deleted in register 45.
    """
    speed_bps = downloaded / elapsed if elapsed > 0 else 0.0
    remaining = total - downloaded
    return {
        "phase": "downloading",
        "percentage": round(downloaded / total * 100, 1) if total else 0,
        "downloaded_bytes": downloaded,
        "total_bytes": total,
        "speed_bps": speed_bps,
        "eta_seconds": (
            int(remaining / speed_bps) if speed_bps > 0 and total > 0 else 0
        ),
    }


def _discard_archive(archive_path: Path | None) -> None:
    """Delete the downloaded archive from the temp download dir.

    Runs from :meth:`GameVaultInstaller.install_game`'s ``finally``, so it
    must never raise: a failure here would replace the real install error
    with an unlink error. Best-effort by design — the archive is a cache,
    and the worst case of leaving one behind is wasted disk, whereas
    propagating would lose the diagnosis of why the install failed.
    """
    if not archive_path or not archive_path.exists():
        return
    try:
        archive_path.unlink()
        logger.info("[GameVaultInstaller] Removed archive %s", archive_path)
    except Exception as exc:
        logger.warning(
            "[GameVaultInstaller] Could not delete archive %s: %s",
            archive_path,
            exc,
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
    except Exception:
        logger.exception("[GameVaultInstaller] Could not save marker")


def _load_install_info(game_id: str) -> dict[str, Any] | None:
    p = _marker_path(game_id)
    try:
        if p.exists():
            loaded = json.loads(p.read_text())
            if isinstance(loaded, dict):
                return loaded
    except Exception as exc:
        logger.debug("[GameVaultInstaller] Could not read marker: %s", exc)
    return None


def _remove_install_info(game_id: str) -> None:
    p = _marker_path(game_id)
    try:
        if p.exists():
            p.unlink()
    except Exception as exc:
        logger.warning("[GameVaultInstaller] Could not remove marker: %s", exc)


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _safe_archive_name(candidate: str | None, game_id: str) -> str:
    """A filename that cannot escape the download directory.

    ``Content-Disposition`` is written by the server, so the name in it is
    remote input: ``../../.ssh/authorized_keys`` is a valid header value.
    ``Path(...).name`` drops every directory component, and the dot-only
    names that survive it (``.``, ``..``) are rejected outright, so the
    result can only ever land directly inside ``download_dir``.
    """
    name = Path(candidate or "").name.strip()
    if not name or set(name) <= {"."}:
        return f"gamevault_{game_id}.bin"
    return name


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
