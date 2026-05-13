"""SteamGridDB API client — fetch artwork for non-Steam shortcuts.

OP-14d | py_modules/unifideck/steam/steamgriddb.py

SteamGridDB hosts community-uploaded artwork (grids,
heroes, logos, icons) for Steam games. The plugin
queries it to populate Steam's grid directory for the
non-Steam shortcuts it adds — without artwork, the
shortcuts look bare in the Steam library.

The four artwork kinds are exposed via the
``ARTWORK_KINDS`` table mapping kind → (endpoint,
preferred dimensions list). Dimensions are advisory —
the API returns whatever it has and the picker chooses
the best match locally.

Two surfaces:

* Module-level functions (``search_artwork``,
  ``fetch_all_kinds``) for one-off use;
* ``SteamGridDBClient`` class for caching the API
  key across calls.

API authentication: Bearer token via the
``Authorization`` header. The key is config-supplied
by the user.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unifideck.utils.config_helpers import get_cfg

if TYPE_CHECKING:
    from ..config import ConfigManager

logger = logging.getLogger(__name__)

SGDB_API_BASE = "https://www.steamgriddb.com/api/v2"
ARTWORK_KINDS = {
    "grid": ("grids", ["600x900"]),
    "hero": ("heroes", ["1920x620", "3840x1240"]),
    "logo": ("logos", None),
    "icon": ("icons", None),
}

@dataclass
class ArtworkAsset:
    """One artwork asset returned by SteamGridDB.

    Attributes:
        url: direct URL of the artwork file.
        width / height: pixel dimensions.
        style: SteamGridDB-defined style tag
            (``"alternate"``, ``"official"``, …).
        mime: image MIME type (``"image/png"`` /
            ``"image/jpeg"``).
        game_id: SteamGridDB internal game id this
            asset belongs to.
    """

    url: str
    width: int
    height: int
    style: str
    mime: str
    game_id: int

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict.

        Returns:
            Six-key dict.
        """
        return {
            "url": self.url,
            "width": self.width,
            "height": self.height,
            "style": self.style,
            "mime": self.mime,
            "game_id": self.game_id,
        }

async def search_artwork(title: str, kind: str, api_key: str | None = None, config: ConfigManager | None = None) -> str | None:
    """Look up ``title`` and return the best asset URL for ``kind``.

    Three-step pipeline:

    1. Validate ``kind`` against ``ARTWORK_KINDS``
       (raises ``ValueError`` on unknown);
    2. ``_search_game`` to resolve the title to a
       SteamGridDB game id;
    3. ``_fetch_assets`` for the right endpoint, then
       ``_pick_best_asset`` to choose.

    Missing API key short-circuits to ``None`` —
    callers should check that before invoking, but
    silent degradation is safer than raising in the
    background-fetch path.

    Args:
        title: human-readable title.
        kind: one of ``ARTWORK_KINDS`` keys.
        api_key: SteamGridDB Bearer token.
        config: optional ``ConfigManager``.

    Returns:
        Direct URL string, or ``None``.

    Raises:
        ValueError: on unknown ``kind``.
    """
    if kind not in ARTWORK_KINDS:
        raise ValueError(f"unknown artwork kind: {kind}")
    if not api_key:
        return None
    base = get_cfg(
        config,
        "artwork.steamgriddb_api_base",
        "https://www.steamgriddb.com/api/v2",
    )
    timeout = get_cfg(config, "artwork.download_timeout_seconds", 30)
    game = await _search_game(title, api_key, base, timeout)
    if game is None:
        return None
    endpoint, _dimensions = ARTWORK_KINDS[kind]
    assets = await _fetch_assets(
        game["id"],
        endpoint,
        api_key,
        base,
        timeout,
    )
    best = _pick_best_asset(assets)
    return best.url if best else None

async def fetch_all_kinds(title: str, api_key: str | None, config: ConfigManager | None = None) -> dict[str, str | None]:
    """Fetch every artwork kind for one title in a single search.

    Optimised over four separate ``search_artwork``
    calls: does the title search once, then fetches
    each endpoint reusing the same game id.

    Returns ``{kind: None, ...}`` skeleton on missing
    API key or no game match — keeps callers from
    having to special-case missing dict keys.

    Args:
        title: human-readable title.
        api_key: Bearer token.
        config: optional ``ConfigManager``.

    Returns:
        Dict from ``ARTWORK_KINDS`` keys to URL
        strings (or ``None`` for missing assets).
    """
    if not api_key:
        return dict.fromkeys(ARTWORK_KINDS)

    base = get_cfg(config, "artwork.steamgriddb_api_base", "https://www.steamgriddb.com/api/v2")
    timeout = get_cfg(config, "artwork.download_timeout_seconds", 30)
    game = await _search_game(title, api_key, base, timeout)

    if game is None:
        return dict.fromkeys(ARTWORK_KINDS)

    game_id = game["id"]
    results: dict[str, str | None] = {}
    for kind, (endpoint, _dims) in ARTWORK_KINDS.items():
        assets = await _fetch_assets(
            game_id,
            endpoint,
            api_key,
            base,
            timeout,
        )
        best = _pick_best_asset(assets)
        results[kind] = best.url if best else None
    return results


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

async def _search_game(title: str, api_key: str, base: str, timeout: int) -> dict[str, Any] | None:
    """Search SteamGridDB autocomplete and return the first hit.

    Defensive: non-200, transport failure, or empty
    payload all return ``None`` (logged at DEBUG).
    The ``success`` flag in the JSON is also checked
    — SteamGridDB returns 200 even for errors but
    flips that flag.

    Args:
        title: search query.
        api_key: Bearer token.
        base: API base URL.
        timeout: HTTP timeout.

    Returns:
        First game dict from the response, or ``None``.
    """
    import aiohttp

    url = f"{base}/search/autocomplete/{title}"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                url,
                headers=headers,
                timeout=timeout,
            ) as resp,
        ):
            if resp.status != 200:
                logger.debug(
                    "[sgdb] search(%s) → HTTP %d",
                    title,
                    resp.status,
                )
                return None
            payload = await resp.json()
    except (aiohttp.ClientError, OSError, ValueError, asyncio.TimeoutError) as e:
        logger.debug(
            "[sgdb] search(%s) failed: %s",
            title,
            e,
        )
        return None
    if not payload.get("success"):
        return None
    data = payload.get("data") or []
    return data[0] if data else None

async def _fetch_assets(game_id: int, endpoint: str, api_key: str, base: str, timeout: int) -> list[ArtworkAsset]:
    """List artwork assets for a given game id + kind endpoint.

    Returns ``[]`` on any failure (transport, non-
    200, ``success=false``). For each asset entry,
    builds an ``ArtworkAsset`` — entries missing the
    required ``url`` field are silently skipped via
    the ``KeyError`` except.

    Args:
        game_id: SteamGridDB game id.
        endpoint: ``"grids"`` / ``"heroes"`` / etc.
        api_key: Bearer token.
        base: API base URL.
        timeout: HTTP timeout.

    Returns:
        List of ``ArtworkAsset`` (may be empty).
    """
    import aiohttp

    url = f"{base}/{endpoint}/game/{game_id}"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                url,
                headers=headers,
                timeout=timeout,
            ) as resp,
        ):
            if resp.status != 200:
                return []
            payload = await resp.json()
    except (aiohttp.ClientError, OSError, ValueError, asyncio.TimeoutError) as e:
        logger.debug(
            "[sgdb] fetch(%d, %s) failed: %s",
            game_id,
            endpoint,
            e,
        )
        return []
    if not payload.get("success"):
        return []
    results: list[ArtworkAsset] = []
    for item in payload.get("data", []):
        try:
            results.append(
                ArtworkAsset(
                    url=item["url"],
                    width=item.get("width", 0),
                    height=item.get("height", 0),
                    style=item.get("style", ""),
                    mime=item.get("mime", "image/png"),
                    game_id=game_id,
                )
            )
        except KeyError:
            continue
    return results

def _pick_best_asset(assets: list[ArtworkAsset]) -> ArtworkAsset | None:
    """Pick the highest-resolution non-alternate asset (with tiebreaker).

    Two-component ranking:

    * **Style rank** — ``style == "alternate"`` wins
      (1 vs 0); SteamGridDB's "alternate" tag marks
      community-favoured submissions vs the
      auto-uploaded defaults.
    * **Resolution** — ``width * height``;
      higher-resolution wins on ties.

    Returns ``None`` for empty input.

    Args:
        assets: list of candidate assets.

    Returns:
        Highest-ranked asset, or ``None``.
    """
    if not assets:
        return None

    def rank(asset: ArtworkAsset) -> tuple:
        """Two-component ranking tuple for ``max``.

        First component dominates: alternate-style
        beats non-alternate at any resolution.
        Resolution is the within-style tiebreaker.

        Args:
            asset: candidate to rank.

        Returns:
            ``(style_rank, resolution)`` tuple.
        """
        style_rank = 1 if asset.style == "alternate" else 0
        res = asset.width * asset.height
        return (style_rank, res)

    return max(assets, key=rank)


class SteamGridDBClient:
    """Stateful wrapper holding the API key across calls."""

    def __init__(self, api_key=None):
        """Bind the API key (immutable for the client's lifetime).

        Args:
            api_key: SteamGridDB Bearer token. ``None``
                disables fetches (calls return ``None``
                / skeletons).
        """
        self.api_key = api_key

    async def search_artwork(self, title, kind, **kwargs):
        """Delegate to the module-level ``search_artwork``.

        ``kwargs`` is accepted for forward
        compatibility but ignored.

        Args:
            title: search query.
            kind: artwork kind.
            **kwargs: ignored.

        Returns:
            URL string or ``None``.
        """
        return await search_artwork(title, kind, self.api_key)

    async def fetch_all_kinds(self, title, **kwargs):
        """Delegate to the module-level ``fetch_all_kinds``.

        Args:
            title: search query.
            **kwargs: ignored.

        Returns:
            ``{kind: url_or_none}`` dict.
        """
        return await fetch_all_kinds(title, self.api_key)
