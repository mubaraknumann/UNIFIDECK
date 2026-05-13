"""Steam install discovery + Store search.

OP-14a | py_modules/unifideck/steam/library.py

Two surfaces:

* **Install discovery** — locate the Steam root on
  disk (``find_steam_path``), the grid dir
  (``find_grid_path``), and the shortcuts.vdf
  (``find_shortcuts_vdf``). All three walk a list of
  conventional install candidates (config-overridable)
  plus the per-user subpath resolved from
  ``loginusers.vdf``.
* **Store search** — query Steam's public storesearch
  API to resolve a title to an AppID
  (``search_store``) or batch (``batch_search_store``).

Used by cross-store deduplication (to detect titles the
user owns natively on Steam), by the shortcut service
(to find ``shortcuts.vdf``), and by the metadata
service (to enrich non-Steam games with Steam's own
metadata).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from ..utils.config_helpers import get_cfg

if TYPE_CHECKING:
    from ..config import ConfigManager

logger = logging.getLogger(__name__)

STEAM_PATH_CANDIDATES = (
    "~/.steam/steam",
    "~/.local/share/Steam",
    "~/.var/app/com.valvesoftware.Steam/.steam/steam",
)
STEAM_STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch"


def find_steam_path(config: ConfigManager | None = None) -> str | None:
    """Locate the Steam install root by checking known candidate paths.

    Walks the configured candidate list (defaults to
    ``STEAM_PATH_CANDIDATES``) and returns the first
    one that contains a ``steamapps/`` subdirectory.
    The presence of ``steamapps/`` is the unambiguous
    marker for a real Steam install (separates from
    accidentally-matching dirs like an empty
    ``~/.steam/``).

    Args:
        config: optional ``ConfigManager``;
            ``paths.steam_candidates`` overrides the
            default list.

    Returns:
        Absolute path string, or ``None`` if no
        candidate matched.
    """
    candidates = get_cfg(
        config,
        "paths.steam_candidates",
        list(STEAM_PATH_CANDIDATES),
    )
    for candidate in candidates:
        expanded = Path(candidate).expanduser()
        if (expanded / "steamapps").is_dir():
            return str(expanded)
    return None


def find_grid_path(steam_path: str | None = None, config: ConfigManager | None = None) -> str | None:
    """Resolve the per-user ``config/grid/`` directory for artwork.

    Three-step:

    1. Resolve the Steam root (passed-in or via
       ``find_steam_path``);
    2. Find the most-recent user id from
       ``loginusers.vdf``;
    3. Build ``<steam>/userdata/<uid>/config/grid``
       and create it if missing.

    Returns ``None`` on any failure (Steam not found,
    no user, mkdir error).

    Args:
        steam_path: optional pre-resolved Steam root.
        config: optional ``ConfigManager``.

    Returns:
        Grid directory path, or ``None``.
    """
    steam = steam_path or find_steam_path(config)
    if not steam:
        return None
    user_id = _find_most_recent_user(steam)
    if not user_id:
        return None
    grid_dir = Path(steam) / "userdata" / user_id / "config" / "grid"
    try:
        grid_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(
            "[steam/library] grid mkdir failed: %s",
            e,
        )
        return None
    return str(grid_dir)

def find_shortcuts_vdf(steam_path: str | None = None, config: ConfigManager | None = None) -> str | None:
    """Resolve the per-user ``shortcuts.vdf`` path.

    Same resolution chain as ``find_grid_path`` but
    points at the shortcuts file rather than the grid
    directory. The file may not exist on disk yet —
    callers handle the missing-file case (typically by
    treating it as "no shortcuts" rather than an
    error).

    Args:
        steam_path: optional pre-resolved Steam root.
        config: optional ``ConfigManager``.

    Returns:
        Shortcut file path, or ``None`` if Steam /
        user resolution failed.
    """
    steam = steam_path or find_steam_path(config)
    if not steam:
        return None
    user_id = _find_most_recent_user(steam)
    if not user_id:
        return None
    return str(Path(steam) / "userdata" / user_id / "config" / "shortcuts.vdf")

def _cfg(config: ConfigManager | None, key: str, default: Any) -> Any:
    """Thin wrapper around ``get_cfg`` — local convention.

    Args:
        config: optional ``ConfigManager``.
        key: dotted config key.
        default: fallback.

    Returns:
        Config value or default.
    """
    return get_cfg(config, key, default)

def _find_most_recent_user(steam_path: str) -> str | None:
    """Extract the most-recently-logged-in 17-digit SteamID from ``loginusers.vdf``.

    Steam stores per-user login data in
    ``config/loginusers.vdf`` with a ``MostRecent``
    flag set to ``"1"`` for the user that was active
    at the last shutdown. The regex looks for an
    entry whose body contains that flag.

    Returns ``None`` on missing file, read error, or
    no ``MostRecent="1"`` entry (Steam was never
    logged in).

    Args:
        steam_path: Steam install root.

    Returns:
        17-digit SteamID string, or ``None``.
    """
    loginusers = Path(steam_path) / "config" / "loginusers.vdf"
    if not loginusers.is_file():
        return None
    try:
        text = loginusers.read_text(encoding="utf-8")
    except OSError:
        return None
    pattern = re.compile(
        r'"(\d{17})"\s*\{[^}]*"MostRecent"\s*"1"',
        re.DOTALL,
    )
    m = pattern.search(text)
    return m.group(1) if m else None

@dataclass
class SteamStoreResult:
    """Typed shape for one ``search_store`` hit.

    Attributes:
        app_id: Steam AppID.
        name: canonical title.
        header_image: small header image URL.
        price: localised price string (e.g. ``"$59.99"``).
        release_date: release date string (empty when
            not provided by the API).
    """

    app_id: int
    name: str
    header_image: str
    price: str
    release_date: str

    def to_dict(self) -> dict:
        """Serialise to a JSON-friendly dict.

        Returns:
            Five-key dict.
        """
        return {
            "app_id": self.app_id,
            "name": self.name,
            "header_image": self.header_image,
            "price": self.price,
            "release_date": self.release_date,
        }

async def search_store(title: str, config: ConfigManager | None = None) -> dict | None:
    """Query Steam's storesearch API and return the top hit.

    Calls ``store.steampowered.com/api/storesearch``
    with English/US locale parameters (the API's
    default — gives the most matches). Returns just
    the first result; callers needing more should use
    a higher-level helper.

    Defensive against transport / parsing failures —
    returns ``None`` on any error (logged at DEBUG).

    Args:
        title: search query.
        config: optional ``ConfigManager`` for URL +
            timeout overrides.

    Returns:
        Dict from ``SteamStoreResult.to_dict``, or
        ``None`` if no hits or error.
    """
    import aiohttp

    url = get_cfg(
        config,
        "metadata.steam_store.search_url",
        STEAM_STORE_SEARCH_URL,
    )
    timeout = get_cfg(
        config,
        "metadata.steam_store.search_timeout_seconds",
        15,
    )
    params = {"term": title, "l": "english", "cc": "US"}
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                url,
                params=params,
                timeout=timeout,
            ) as resp,
        ):
            if resp.status != 200:
                return None
            data = await resp.json()
    except (aiohttp.ClientError, OSError, ValueError, asyncio.TimeoutError) as e:
        logger.debug(
            "[steam/library] search(%s) failed: %s",
            title,
            e,
        )
        return None
    items = data.get("items") or []
    if not items:
        return None
    item = items[0]
    price = ""
    if isinstance(item.get("price"), dict):
        price = item["price"].get("final", "")
    return SteamStoreResult(
        app_id=int(item.get("id", 0)),
        name=item.get("name", ""),
        header_image=item.get("tiny_image", ""),
        price=str(price),
        release_date="",
    ).to_dict()

async def batch_search_store(titles: list[str]) -> dict:
    """Search every title sequentially and return a ``{title: result}`` dict.

    Sequential rather than parallel to be courteous to
    Steam's API. Each entry maps to the search result
    dict (or ``None`` on no-hit).

    Args:
        titles: list of query titles.

    Returns:
        Dict from title to result.
    """
    results = {}
    for title in titles:
        results[title] = await search_store(title)
    return results
