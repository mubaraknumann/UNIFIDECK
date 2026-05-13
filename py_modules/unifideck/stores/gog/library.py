"""GOG library reader — fetch the user's owned games + match local installs.

OP-22-gog-library | py_modules/unifideck/stores/gog/library.py

Three responsibilities:

1. **Auth verification** — ``is_available``
   probes ``/userData.json`` to confirm tokens
   are valid, with a 401-refresh-retry loop;
2. **Library fetch** — paginated walk through
   ``/account/getFilteredProducts`` to build the
   list of owned games;
3. **Install detection** — scan the download dir
   for ``.unifideck-id`` markers (preferred) or
   goggame info files (fallback) to identify
   installed games.

The library API doesn't expose install paths —
those are discovered by scanning the local disk
in ``get_installed`` / ``get_installed_game_info``.

The marker migration helper (``_MarkerMigration``)
is invoked from ``migrate_old_markers`` — typically
called once at first library sync per session.

URL safety: ``is_available`` refuses to probe a
non-HTTPS userdata URL — defensive measure
against misconfigured ``base_url`` that could
otherwise leak the bearer token in clear text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from ...core.types import Game
from .config import GOGConfig
from .http import build_ssl_context, fetch_json_get
from .library_migration import _MarkerMigration
from .tokens import GOGTokenManager

logger = logging.getLogger(__name__)

_INSTALL_MARKER = ".unifideck-id"
_GOG_LIBRARY_TIMEOUT_S = 15.0


class GOGLibrary:
    """Read-only library + install detector for the GOG store.

    Holds the config, tokens, and exe-finder
    callable. The migration helper is created at
    construction time but only runs when
    ``migrate_old_markers`` is invoked.
    """

    def __init__(
        self,
        config: GOGConfig,
        tokens: GOGTokenManager,
        exe_finder: Callable[[str], str | None] | None = None,
    ) -> None:
        """Stash dependencies + build the migration helper.

        Args:
            config: ``GOGConfig``.
            tokens: ``GOGTokenManager``.
            exe_finder: optional callable
                resolving exe paths (typically
                ``GOGExeResolver.find``). When
                ``None``, install info is
                returned without an executable.
        """
        self._config = config
        self._tokens = tokens
        self._find_exe = exe_finder
        self._migration = _MarkerMigration(self)

    def migrate_old_markers(self) -> dict[str, int]:
        """Run the one-shot marker migration. Returns count summary.

        Returns:
            ``{"migrated": int, "skipped": int}``.
        """
        return self._migration.migrate_old_markers()

    async def is_available(self) -> bool:
        """Probe ``/userData.json`` to verify tokens are valid (with 401 refresh).

        Pipeline:

        1. If no tokens in memory, try
           ``load()`` from disk; still no →
           False;
        2. Probe userdata;
        3. 200 → True;
        4. 401 → refresh + retry once;
        5. Other status → False (logged at WARN).

        Returns:
            True iff the auth probe succeeded.
        """
        if not self._tokens.has_tokens:
            loaded = await self._tokens.load()
            if not loaded:
                logger.info(
                    "[GOGLibrary] no tokens — not authenticated",
                )
                return False
        status = await self._probe_userdata()
        if status == 200:
            return True
        if status == 401:
            logger.warning(
                "[GOGLibrary] token expired (401), refreshing",
            )
            ok = await self._tokens.refresh_if_stale()
            if ok:
                status = await self._probe_userdata()
                return status == 200
            return False
        logger.warning(
            "[GOGLibrary] userdata probe returned %s",
            status,
        )
        return False

    async def _probe_userdata(self) -> int:
        """Single auth probe — return HTTP status code (0 on network error).

        Refuses non-HTTPS URLs as a defensive
        measure — without this check, a
        misconfigured ``base_url`` could leak
        the bearer token in clear text.

        5-second timeout (this is a fast
        liveness check, not a normal call).

        Returns:
            HTTP status, or 0 on
            error/refused.
        """
        url = f"{self._config.base_url}/userData.json"
        access = self._tokens.access_token
        if not access:
            return 0
        if not url.startswith("https://"):
            logger.error(
                "[GOGLibrary] refusing non-https probe URL: %s",
                url,
            )
            return 0

        def _probe_sync() -> int:
            """Blocking urllib probe — returns status code or 0 on error.

            Catches HTTPError to extract its code
            (not all status codes raise) and
            falls through to 0 on any other
            exception.
            """
            try:
                ctx = build_ssl_context()
                req = urllib.request.Request(
                    url,
                    headers={
                        "Authorization": f"Bearer {access}",
                        "User-Agent": self._config.user_agent,
                    },
                )
                with urllib.request.urlopen(
                    req,
                    timeout=5.0,
                    context=ctx,
                ) as response:
                    return cast("int", response.status)
            except urllib.request.HTTPError as e:
                return e.code
            except Exception as e:
                logger.debug(
                    "[GOGLibrary] probe error: %s",
                    e,
                )
                return 0

        return await asyncio.to_thread(_probe_sync)

    async def fetch_library(self) -> list[Game]:
        """Paginated walk through the GOG library API, returning all owned games.

        First page reveals ``totalPages`` and
        ``totalGamesFound`` (logged at INFO).
        Subsequent pages walk until exhausted.

        Per-page failure → log + break (we return
        whatever we managed to collect). Empty/
        absent ``products`` field per page just
        means an empty page, not an error.

        ``Game`` objects are built with
        ``installed=False`` — installed status
        is reconciled later by the store via
        ``get_installed``.

        Returns:
            All ``Game`` objects, possibly empty.
        """
        if not self._tokens.access_token:
            logger.warning("[GOGLibrary] not authenticated")
            return []
        games: list[Game] = []
        current_page = 1
        total_pages = 1
        base_url = self._config.base_url
        while current_page <= total_pages:
            url = (
                f"{base_url}/account/getFilteredProducts?"
                f"mediaType=1&page={current_page}"
            )
            data = await self._fetch_json(url)
            if data is None:
                logger.error(
                    "[GOGLibrary] page %d failed, stopping",
                    current_page,
                )
                break
            if current_page == 1:
                total_pages = int(
                    data.get("totalPages", 1) or 1,
                )
                total_results = int(
                    data.get("totalGamesFound", 0) or 0,
                )
                logger.info(
                    "[GOGLibrary] library has %d games across %d pages",
                    total_results,
                    total_pages,
                )
            for product in data.get("products", []):
                game_id = str(product.get("id", ""))
                if not game_id:
                    continue
                games.append(
                    Game(
                        app_id=0,
                        store="gog",
                        store_game_id=game_id,
                        title=product.get("title", "") or "",
                        installed=False,
                    )
                )
            current_page += 1
        logger.info(
            "[GOGLibrary] fetched %d games total",
            len(games),
        )
        return games

    async def get_game_slug(self, game_id: str) -> str | None:
        """Resolve a game's URL slug (used for storefront / DLC URLs).

        Two-stage:

        1. ``/products/<id>?locale=en-US``
           returns a ``slug`` field directly;
        2. Fallback: parse the
           ``links.product_card`` URL for the
           slug after ``/game/``.

        Returns ``None`` on any failure
        (auth refresh fail, non-dict response,
        missing slug).

        Args:
            game_id: product id.

        Returns:
            Slug or ``None``.
        """
        if not await self._tokens.refresh_if_stale():
            return None
        access = self._tokens.access_token
        if not access:
            return None
        url = f"{self._config.api_gog_url}/products/{game_id}?locale=en-US"
        data = await self._fetch_json(
            url,
            headers={
                "Authorization": f"Bearer {access}",
                "User-Agent": self._config.user_agent,
            },
        )
        if not isinstance(data, dict):
            return None
        slug = data.get("slug")
        if isinstance(slug, str) and slug:
            return slug
        links = data.get("links", {})
        if isinstance(links, dict):
            product_card = links.get("product_card", "")
            if isinstance(product_card, str) and "/game/" in product_card:
                return product_card.split("/game/")[-1].rstrip("/")
        return None

    def get_installed(self) -> list[str]:
        """Scan the download dir for ``.unifideck-id`` markers; return game IDs.

        Iterates top-level subdirs of the
        download path; each subdir with a
        valid marker contributes its game id.

        Missing download dir → empty list
        (no error). OSError during scan → log +
        empty list.

        Returns:
            List of installed game ids.
        """
        download_path = Path(
            self._config.download_dir,
        ).expanduser()
        if not download_path.is_dir():
            return []
        installed: list[str] = []
        try:
            for entry in download_path.iterdir():
                if not entry.is_dir():
                    continue
                game_id = self._read_marker(str(entry))
                if game_id:
                    installed.append(game_id)
        except OSError as e:
            logger.error(
                "[GOGLibrary] get_installed scan failed: %s",
                e,
            )
            return []
        logger.info(
            "[GOGLibrary] found %d installed games",
            len(installed),
        )
        return installed

    def get_installed_game_info(self, game_id: str) -> dict[str, str | None] | None:
        """Find install path + exe for a specific game id.

        Two match strategies:

        1. ``.unifideck-id`` marker matches the
           requested ``game_id`` — primary path;
        2. No marker found but a goggame info
           file with matching id exists in the
           dir — fallback for cases where the
           marker was deleted but the install
           survived.

        Returns ``None`` if neither match — game
        isn't installed.

        Args:
            game_id: product id.

        Returns:
            ``{install_path, executable}`` dict
            or ``None``.
        """
        download_path = Path(
            self._config.download_dir,
        ).expanduser()
        if not download_path.is_dir():
            return None
        try:
            for entry in download_path.iterdir():
                if not entry.is_dir():
                    continue
                game_dir = str(entry)
                found = self._read_marker(game_dir)
                if found == game_id:
                    return {
                        "install_path": game_dir,
                        "executable": self._resolve_exe(game_dir),
                    }
                if found is None and self._has_goggame_info(
                    game_dir,
                    game_id,
                ):
                    logger.info(
                        "[GOGLibrary] found %s via goggame info fallback at %s",
                        game_id,
                        game_dir,
                    )
                    return {
                        "install_path": game_dir,
                        "executable": self._resolve_exe(game_dir),
                    }
        except OSError as e:
            logger.error(
                "[GOGLibrary] get_installed_game_info: %s",
                e,
            )
        return None

    @staticmethod
    def _read_marker(game_dir: str) -> str | None:
        """Parse a ``.unifideck-id`` marker file — handles legacy + new formats.

        Three valid shapes:

        * JSON dict with ``game_id`` or
          ``gameId`` field (new format);
        * JSON string/number (legacy interim);
        * Raw text (oldest legacy).

        Empty file or unreadable → ``None``.
        Falls through to raw-text fallback on
        any JSON parse error.

        Args:
            game_dir: install dir.

        Returns:
            Game id or ``None``.
        """
        marker_path = Path(game_dir) / _INSTALL_MARKER
        if not marker_path.is_file():
            return None
        try:
            content = marker_path.read_text(
                encoding="utf-8",
            ).strip()
        except OSError as e:
            logger.warning(
                "[GOGLibrary] marker read failed: %s",
                e,
            )
            return None
        if not content:
            return None
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                return data.get("game_id") or data.get("gameId")
            if isinstance(data, (str, int)):
                return str(data)
        except json.JSONDecodeError:
            pass
        return content

    @staticmethod
    def _has_goggame_info(game_dir: str, game_id: str) -> bool:
        """Check for ``goggame-<id>.info`` in the install dir or its ``game/`` subdir.

        Mirrors the search pattern used
        throughout the GOG codebase: try the root
        first, then the ``game/`` subdirectory.

        Args:
            game_dir: install root.
            game_id: product id.

        Returns:
            True iff the marker file exists.
        """
        for candidate in (game_dir, str(Path(game_dir) / "game")):
            try:
                if not Path(candidate).is_dir():
                    continue
                target = f"goggame-{game_id}.info"
                if (Path(candidate) / target).is_file():
                    return True
            except OSError:
                continue
        return False

    def _resolve_exe(self, install_path: str) -> str | None:
        """Call the injected exe-finder, defensively swallow exceptions.

        The exe-finder is third-party callable
        territory (typically
        ``GOGExeResolver.find``). We don't want
        a resolver bug to crash library
        reading, so we catch + log.

        Args:
            install_path: install root.

        Returns:
            Exe path, or ``None``.
        """
        if self._find_exe is None:
            return None
        try:
            return self._find_exe(install_path)
        except Exception as e:
            logger.warning(
                "[GOGLibrary] exe resolution failed: %s",
                e,
            )
            return None

    async def _fetch_json(self, url: str, headers: dict[str, str] | None = None) -> Any | None:
        """Thin wrapper around ``fetch_json_get`` with library defaults.

        Sets bearer to the current access token,
        UA from config, 15-second timeout, log
        prefix ``[GOGLibrary]``.

        Args:
            url: target URL.
            headers: extra headers (merged on top
                of defaults).

        Returns:
            Parsed JSON or ``None``.
        """
        return await fetch_json_get(
            url,
            bearer=self._tokens.access_token,
            user_agent=self._config.user_agent,
            timeout=_GOG_LIBRARY_TIMEOUT_S,
            extra_headers=headers,
            log_prefix="[GOGLibrary]",
        )
