"""
Crowd-sourced Ubisoft game-ID lookup tables — download & cache.

OP-55h | py_modules/unifideck/stores/ubisoft/id_map_sources.py

Ubisoft does not publish a public mapping from UPC ``space_id`` to
human-readable game name; we rely on a community-maintained list hosted
on GitHub (``iArtorias/ubisoft_game_ids``).

This module:

* downloads and caches the list with TTL (``game_id_db_max_age_seconds``);
* parses the file (one ``space_id|name`` per line) into a dict;
* exposes ``lookup(space_id)`` for the library facade to call when it
  finds an installed game whose name isn't in the local UPC catalog.

Network failures are degraded gracefully — a stale cache is preferred
to no cache, and a missing cache is preferred to a hard error: in both
cases the lookup falls back to an empty dict and the library facade
displays "Ubisoft Game" as a placeholder name.
"""

from __future__ import annotations
import asyncio
import logging
import re
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any
from ...core.net import ssl_ctx_permissive

if TYPE_CHECKING:
    from .id_map import UbisoftIdMap
logger = logging.getLogger(__name__)
_REGISTRY_INSTALLS_PATTERN = re.compile(
    r"\[Software\\\\Wow6432Node\\\\Ubisoft\\\\Launcher"
    r"\\\\Installs\\\\(\d+)\]"
    r'[^\[]*?"InstallDir"\s*=\s*"([^"]*)"',
    re.DOTALL,
)
_USER_REG_INSTALLS_PATTERN = re.compile(
    r"\[Software\\\\Ubisoft\\\\Launcher\\\\Installs\\\\(\d+)\]",
)
_STANDARD_INSTALL_PATH_MARKERS = (
    "Ubisoft Game Launcher/games/",
    "Ubisoft Game Launcher\\games\\",
)


def extract_game_id_from_registry(
    prefix_path: str,
) -> str | None:
    """Pull the UPC install-id out of a prefix's Wine registry.

    Tries ``<prefix>/system.reg`` then ``<prefix>/pfx/system.reg``;
    within each, prefers ``Software\\Wow6432Node\\...\\Installs\\<id>``
    and falls back to ``user.reg`` sibling scans.

    Args:
        prefix_path: Wine prefix root.

    Returns:
        Install-id string, or ``None`` if no installs entry
        is present in either reg file.
    """
    prefix = Path(prefix_path)
    for reg_name in ("system.reg", "pfx/system.reg"):
        reg_path = prefix / reg_name
        if not reg_path.is_file():
            continue
        content = read_reg_file(str(reg_path))
        if content is None:
            continue
        system_id = scan_system_reg_installs(content)
        if system_id:
            return system_id
        user_id = extract_id_from_user_reg_sibling(
            str(reg_path),
        )
        if user_id:
            return user_id
    return None


def read_reg_file(reg_path: str) -> str | None:
    """Read a Wine registry file with permissive decoding.

    Args:
        reg_path: Absolute path.

    Returns:
        File text, or ``None`` on read failure.
    """
    try:
        return Path(reg_path).read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None


def scan_system_reg_installs(content: str) -> str | None:
    """Find the best Ubisoft install_id in a system.reg dump.

    Prefers entries whose InstallDir contains one of the
    standard UPC games-dir path markers; falls back to the
    first match otherwise (custom install path).

    Args:
        content: system.reg text.

    Returns:
        Install-id, or ``None`` if no Installs block found.
    """
    fallback_id: str | None = None
    for match in _REGISTRY_INSTALLS_PATTERN.finditer(content):
        game_id = match.group(1)
        install_dir = match.group(2).replace("\\\\", "/")
        is_standard = any(
            marker in install_dir for marker in _STANDARD_INSTALL_PATH_MARKERS
        )
        if is_standard:
            logger.info(
                "[UbisoftIdMap] registry ID %s (standard path)",
                game_id,
            )
            return game_id
        if fallback_id is None:
            fallback_id = game_id
    if fallback_id:
        logger.info(
            "[UbisoftIdMap] registry ID %s (custom install path)",
            fallback_id,
        )
        return fallback_id
    return None


def extract_id_from_user_reg_sibling(
    reg_path: str,
) -> str | None:
    """Read an Installs entry from the ``user.reg`` next to a ``system.reg``.

    Args:
        reg_path: Path to a ``system.reg``.

    Returns:
        Install-id, or ``None`` if no sibling user.reg or no
        matching entry.
    """
    user_reg = reg_path.replace("system.reg", "user.reg")
    if not Path(user_reg).is_file():
        return None
    user_content = read_reg_file(user_reg)
    if user_content is None:
        return None
    user_match = _USER_REG_INSTALLS_PATTERN.search(
        user_content,
    )
    if user_match:
        game_id = user_match.group(1)
        logger.info(
            "[UbisoftIdMap] registry ID %s (user.reg)",
            game_id,
        )
        return game_id
    return None


class _IdMapSources:
    """External sources feeding the Ubisoft id_map.

    Two sources: UPC's binary ``configurations`` file (parsed
    via ``parser_binary``), and a community-maintained
    ``space_id|name`` database fetched from GitHub. Both feed
    the in-memory id_map cache through ``update_bulk``.
    """

    def __init__(self, idmap: UbisoftIdMap) -> None:
        """Bind the sources helper to its owning id_map store.

        Args:
            idmap: Parent ``UbisoftIdMap`` instance.
        """
        self._idmap = idmap

    async def refresh_from_configurations(
        self,
        space_id: str | None = None,
    ) -> bool:
        """Re-parse UPC's configurations file and refresh the id_map.

        Tries the template prefix's configurations first; falls
        back to scanning every game prefix for one that has a
        configurations file. Returns ``False`` if none were
        found or all parses produced empty maps.

        Args:
            space_id: Reserved (unused).

        Returns:
            True iff the id_map was refreshed.
        """
        try:
            from ..ubisoft_parser import (
                build_id_map_from_configurations,
            )
        except ImportError as e:
            logger.warning(
                "[UbisoftIdMap] ubisoft_parser unavailable: %s",
                e,
            )
            return False
        config = self._idmap._config
        paths = self._idmap._paths
        template_dir = config.template_dir_expanded
        config_path = paths.find_configurations(template_dir)
        if config_path and await self._refresh_from_path(
            config_path,
            build_id_map_from_configurations,
            "template",
        ):
            return True
        prefixes_dir = Path(config.prefixes_dir_expanded)
        if not prefixes_dir.is_dir():
            logger.info(
                "[UbisoftIdMap] no configurations found in any prefix",
            )
            return False
        try:
            entries = list(prefixes_dir.iterdir())
        except OSError:
            return False
        for entry in entries:
            if entry.name.startswith("."):
                continue
            config_path = paths.find_configurations(
                str(entry),
            )
            if not config_path:
                continue
            if await self._refresh_from_path(
                config_path,
                build_id_map_from_configurations,
                f"prefix {entry.name}",
            ):
                return True
        logger.info(
            "[UbisoftIdMap] no configurations found in any prefix",
        )
        return False

    async def _refresh_from_path(
        self,
        config_path: str,
        parser_fn: Any,
        label: str,
    ) -> bool:
        """Parse one configurations file and merge it into the id_map.

        Args:
            config_path: Absolute path to a UPC configurations file.
            parser_fn: Parser callable (``build_id_map_from_configurations``).
            label: Free-form label for diagnostic logs.

        Returns:
            True iff parsing succeeded and the map was non-empty.
        """
        try:
            new_map = await asyncio.to_thread(
                parser_fn,
                config_path,
            )
        except Exception as e:
            logger.warning(
                "[UbisoftIdMap] parser failed for %s: %s",
                label,
                e,
            )
            return False
        if not new_map:
            return False
        before_count = len(self._idmap._cache)
        self._idmap.update_bulk(new_map)
        after_count = len(self._idmap._cache)
        logger.info(
            "[UbisoftIdMap] refreshed from %s: %d entries (was %d)",
            label,
            after_count,
            before_count,
        )
        return True

    async def fetch_game_id_database(
        self,
    ) -> list[tuple[str, str]]:
        """Fetch (and cache) the community-maintained Ubisoft game-ID list.

        Uses a TTL cache (``game_id_db_max_age_seconds``). When
        fresh cache is missing and the download fails, returns
        an empty list rather than raising.

        Returns:
            List of ``(install_id, name)`` tuples.
        """
        config = self._idmap._config
        cache_file = config.game_id_db_file_expanded
        max_age = config.game_id_db_max_age_seconds
        cache_p = Path(cache_file)
        if cache_p.is_file():
            try:
                age = time.time() - cache_p.stat().st_mtime
                if age < max_age:
                    return await asyncio.to_thread(
                        _parse_game_id_database,
                        cache_file,
                    )
            except OSError:
                pass
        try:
            await asyncio.to_thread(
                _download_game_id_database,
                config.game_id_db_url,
                cache_file,
            )
            logger.info(
                "[UbisoftIdMap] game ID database downloaded",
            )
        except Exception as e:
            logger.warning(
                "[UbisoftIdMap] game ID database download failed: %s",
                e,
            )
            if not cache_p.is_file():
                return []
        return await asyncio.to_thread(
            _parse_game_id_database,
            cache_file,
        )

    async def lookup_game_id_by_name(
        self,
        game_name: str,
    ) -> str | None:
        """Resolve an install_id from a game name via the community DB.

        Matches with the standard id_map name normalization.

        Args:
            game_name: Display name to look up.

        Returns:
            Install-id string, or ``None`` on miss / fetch error.
        """
        if not game_name:
            return None
        try:
            db_entries = await self.fetch_game_id_database()
        except Exception as e:
            logger.debug(
                "[UbisoftIdMap] fetch failed for name lookup: %s",
                e,
            )
            return None
        if not db_entries:
            return None
        normalized_query = self._idmap._normalize_for_matching(game_name)
        for install_id, db_name in db_entries:
            if (
                self._idmap._normalize_for_matching(
                    db_name,
                )
                == normalized_query
            ):
                logger.info(
                    "[UbisoftIdMap] DB match for '%s': ID %s",
                    game_name,
                    install_id,
                )
                return install_id
        return None


def _download_game_id_database(
    url: str,
    dest_path: str,
) -> None:
    """Stream-download the game-ID database to a temp file then atomically rename.

    Uses a permissive SSL context — the upstream CDN ships
    stale certificates and the payload is treated as
    advisory.

    Args:
        url: Source URL.
        dest_path: Final destination path.
    """
    dest_p = Path(dest_path)
    tmp_path = dest_p.with_suffix(dest_p.suffix + ".tmp")
    ctx = ssl_ctx_permissive(
        "Ubisoft community game ID database — CDN has known "
        "stale certs, payload treated as advisory only",
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Unifideck/1.0"},
    )
    dest_p.parent.mkdir(parents=True, exist_ok=True)
    with (
        urllib.request.urlopen(
            req,
            timeout=30.0,
            context=ctx,
        ) as response,
        tmp_path.open("wb") as f,
    ):
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            f.write(chunk)
    tmp_path.replace(dest_p)


def _parse_game_id_database(
    filepath: str,
) -> list[tuple[str, str]]:
    """Parse the ``id, name`` line-format used by the community DB.

    Lines starting with ``#`` are treated as comments and
    skipped. Lines whose ID part isn't all-digits are skipped
    silently.

    Args:
        filepath: Absolute path to the cached DB file.

    Returns:
        List of ``(install_id, name)`` tuples.
    """
    entries: list[tuple[str, str]] = []
    try:
        content = Path(filepath).read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        logger.warning(
            "[UbisoftIdMap] database parse failed: %s",
            e,
        )
        return entries
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(", ", 1)
        if len(parts) == 2 and parts[0].isdigit():
            entries.append((parts[0], parts[1]))
    return entries
