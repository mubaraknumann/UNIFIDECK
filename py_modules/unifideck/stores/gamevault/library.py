"""GameVault library reader — fetches paginated game list from the server."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Game

if TYPE_CHECKING:
    from .install import GameVaultInstaller

logger = logging.getLogger(__name__)

_PAGE_SIZE = 500          # fetch 500 per page (nestjs-paginate allows unlimited)
_MAX_GAMES = 5_000        # sanity cap — no home server has >5000 games


class GameVaultLibraryReader:
    """Reads the game list from a self-hosted GameVault server."""

    def __init__(self, installer: "GameVaultInstaller") -> None:
        self._installer = installer

    # ── Public API ──────────────────────────────────────────────────

    async def get_library(
        self,
        server_url: str,
        auth_headers: dict[str, str],
        verify_ssl: bool,
        *,
        force: bool = False,
    ) -> list[Game]:
        raw_games = await self._fetch_all_pages(server_url, auth_headers, verify_ssl)
        games: list[Game] = []
        for item in raw_games:
            game = self._map_to_game(item)
            if game:
                install_info = self._installer.get_install_info(game.store_game_id)
                if install_info:
                    game.installed = True
                    game.install_path = install_info.get("install_path")
                    game.exe_path = install_info.get("exe_path")
                games.append(game)
        logger.info("[GameVaultLibrary] %d game(s) fetched", len(games))
        return games

    # ── Internal helpers ────────────────────────────────────────────

    async def _fetch_all_pages(
        self,
        server_url: str,
        auth_headers: dict[str, str],
        verify_ssl: bool,
    ) -> list[dict[str, Any]]:
        all_items: list[dict[str, Any]] = []
        offset = 0
        total_pages: int | None = None

        import aiohttp  # noqa: PLC0415 — lazy import, aiohttp vendored
        connector = aiohttp.TCPConnector(ssl=verify_ssl)
        async with aiohttp.ClientSession(connector=connector) as session:
            while True:
                url = (
                    f"{server_url}/api/games"
                    f"?limit={_PAGE_SIZE}&offset={offset}"
                )
                try:
                    async with session.get(
                        url,
                        headers=auth_headers,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        if resp.status != 200:
                            logger.warning(
                                "[GameVaultLibrary] HTTP %s on page offset=%d",
                                resp.status,
                                offset,
                            )
                            break
                        data = await resp.json()
                except Exception as exc:  # noqa: BLE001
                    logger.error("[GameVaultLibrary] Fetch error: %s", exc)
                    break

                # nestjs-paginate returns { data: [...], meta: { totalItems, totalPages, ... } }
                # Fall back to plain list for older API versions.
                if isinstance(data, list):
                    page = data
                else:
                    # Extract pagination metadata on first page
                    if total_pages is None and isinstance(data, dict):
                        meta = data.get("meta", {})
                        total_items = meta.get("totalItems", 0)
                        if total_items > _MAX_GAMES:
                            logger.error(
                                "[GameVaultLibrary] Server reports %d total games "
                                "(max allowed: %d). Check your server_url — "
                                "you may be pointing at a public demo server.",
                                total_items,
                                _MAX_GAMES,
                            )
                            break
                        total_pages = meta.get("totalPages")

                    page = data.get("data", data.get("results", []))

                if not page:
                    break

                all_items.extend(page)

                # Stop if we've hit the sanity cap
                if len(all_items) >= _MAX_GAMES:
                    logger.warning(
                        "[GameVaultLibrary] Hit max-games cap (%d); stopping pagination.",
                        _MAX_GAMES,
                    )
                    break

                # Stop if nestjs-paginate told us the total page count
                if total_pages is not None:
                    current_page = offset // _PAGE_SIZE + 1
                    if current_page >= total_pages:
                        break
                elif len(page) < _PAGE_SIZE:
                    # Fallback: last page is smaller than requested
                    break

                offset += _PAGE_SIZE

        return all_items

    def _map_to_game(self, item: dict[str, Any]) -> Game | None:
        """Convert a raw API game dict to a unified ``Game`` record."""
        try:
            gv_id = str(item.get("id", ""))
            if not gv_id:
                return None

            title = self._extract_title(item)
            icon_url = self._extract_cover_url(item)

            return Game(
                app_id=0,               # filled later by sync service
                store="gamevault",
                store_game_id=gv_id,
                title=title,
                installed=False,        # overridden by get_library()
                icon_url=icon_url,
                metadata={
                    "file_path": item.get("file_path", ""),
                    "release_date": item.get("release_date", ""),
                    "early_access": item.get("early_access", False),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[GameVaultLibrary] map_to_game error: %s", exc)
            return None

    @staticmethod
    def _extract_title(item: dict[str, Any]) -> str:
        """Prefer IGDB/metadata title, then parse from file_path."""
        # New API: metadata.title → boxart.title → file_path parsing
        metadata = item.get("metadata") or {}
        if isinstance(metadata, dict):
            title = metadata.get("title") or metadata.get("name", "")
            if title:
                return title

        # Legacy / fallback: derive from file path
        file_path = item.get("file_path") or item.get("path", "")
        if file_path:
            return _parse_title_from_filename(file_path)

        return f"GameVault Game #{item.get('id', '?')}"

    @staticmethod
    def _extract_cover_url(item: dict[str, Any]) -> str | None:
        """Try several known cover fields."""
        for field in ("cover_image", "cover", "thumbnail"):
            val = item.get(field)
            if val and isinstance(val, str):
                return val
        # Structured boxart
        boxart = item.get("boxart") or item.get("metadata", {}) or {}
        if isinstance(boxart, dict):
            for field in ("url", "background_url", "cover_url"):
                val = boxart.get(field)
                if val and isinstance(val, str):
                    return val
        return None


# ── Standalone utility ──────────────────────────────────────────────────────

def _parse_title_from_filename(file_path: str) -> str:
    """Derive a human-readable title from a GameVault archive filename.

    GameVault filenames follow a loose convention:
        ``Title Name (YEAR).ext``  or  ``Title Name.ext``

    1. Strip directory prefix.
    2. Strip the extension and everything after a ``(YEAR)`` token.
    3. Replace separators with spaces, strip leading/trailing whitespace.
    """
    import os

    name = os.path.basename(file_path)
    # Strip extension
    for ext in (".exe", ".zip", ".rar", ".7z", ".tar.gz", ".tar.bz2"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    # Remove trailing (YEAR) / [YEAR]
    name = re.sub(r"[\(\[]\d{4}[\)\]].*$", "", name).strip()
    # Replace underscores/dashes used as word separators
    name = re.sub(r"[_\-]+", " ", name).strip()
    # Collapse multiple spaces
    name = re.sub(r"\s{2,}", " ", name)
    return name or file_path
