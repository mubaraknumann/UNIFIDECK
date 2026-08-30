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


class GameVaultFetchError(RuntimeError):
    """The server could not be read, so the library answer is unknown.

    Distinct from "the user owns nothing": an empty list is a real answer
    the reconcile acts on, and acting on a failed fetch is what removes a
    user's shortcuts.
    """


class GameVaultLibraryReader:
    """Reads the game list from a self-hosted GameVault server."""

    def __init__(self, installer: GameVaultInstaller) -> None:
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
        """Every owned game, or raise if the server could not be read.

        **Never returns a short list on failure.** A store that answers a
        sync with fewer games than the user owns is indistinguishable from
        one whose library shrank, and the shortcut reconcile believes it:
        the missing games' shortcuts get swept. A page that fails therefore
        aborts the whole fetch (``GameVaultFetchError``), which
        ``GameVaultStore.get_library`` turns into ``None`` — the documented
        "could not answer" signal that leaves the existing shortcuts alone.
        """
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

        import aiohttp
        connector = aiohttp.TCPConnector(ssl=verify_ssl)
        async with aiohttp.ClientSession(connector=connector) as session:
            while True:
                url = (
                    f"{server_url}/api/games"
                    f"?limit={_PAGE_SIZE}&offset={offset}"
                )
                data = await _read_page(session, url, auth_headers, offset)
                page, page_total = _unwrap_page(data, want_meta=total_pages is None)
                if page_total is not None:
                    total_pages = page_total

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
        except Exception as exc:
            logger.debug("[GameVaultLibrary] map_to_game error: %s", exc)
            return None

    @staticmethod
    def _extract_title(item: dict[str, Any]) -> str:
        """Prefer IGDB/metadata title, then parse from file_path."""
        # New API: metadata.title → boxart.title → file_path parsing
        metadata = item.get("metadata") or {}
        if isinstance(metadata, dict):
            title = metadata.get("title") or metadata.get("name", "")
            if isinstance(title, str) and title:
                return title

        # Legacy / fallback: derive from file path
        file_path = item.get("file_path") or item.get("path", "")
        if isinstance(file_path, str) and file_path:
            return _parse_title_from_filename(file_path)

        return f"GameVault Game #{item.get('id', '?')}"

    @staticmethod
    def _extract_cover_url(item: dict[str, Any]) -> str | None:
        """Try several known cover fields."""
        for field in ("cover_image", "cover", "thumbnail"):
            val = item.get(field)
            if isinstance(val, str) and val:
                return val
        # Structured boxart
        boxart = item.get("boxart") or item.get("metadata", {}) or {}
        if isinstance(boxart, dict):
            for field in ("url", "background_url", "cover_url"):
                val = boxart.get(field)
                if isinstance(val, str) and val:
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


async def _read_page(
    session: Any, url: str, auth_headers: dict[str, str], offset: int,
) -> Any:
    """One page of ``/api/games``, or ``GameVaultFetchError``.

    Every failure becomes that one exception so the caller cannot mistake a
    dead server for a short library — see ``get_library``'s docstring.
    """
    import aiohttp
    try:
        async with session.get(
            url, headers=auth_headers, timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                raise GameVaultFetchError(
                    f"server returned HTTP {resp.status} for offset={offset}",
                )
            return await resp.json()
    except GameVaultFetchError:
        raise
    except Exception as exc:
        raise GameVaultFetchError(
            f"could not read page at offset={offset}: {exc}",
        ) from exc


def _unwrap_page(
    data: Any, *, want_meta: bool,
) -> tuple[list[dict[str, Any]], int | None]:
    """``(items, total_pages)`` from either API shape.

    nestjs-paginate answers ``{data: [...], meta: {...}}``; older servers
    answer a bare list. ``total_pages`` is ``None`` unless *want_meta* and
    the payload carried it.
    """
    if isinstance(data, list):
        return data, None
    if not isinstance(data, dict):
        return [], None
    total_pages: int | None = None
    if want_meta:
        meta = data.get("meta", {})
        total_items = meta.get("totalItems", 0) if isinstance(meta, dict) else 0
        if total_items > _MAX_GAMES:
            raise GameVaultFetchError(
                f"server reports {total_items} games, over the {_MAX_GAMES} "
                f"sanity cap — check server_url, it may be a public demo server",
            )
        if isinstance(meta, dict):
            total_pages = meta.get("totalPages")
    page = data.get("data", data.get("results", []))
    return (page if isinstance(page, list) else []), total_pages
