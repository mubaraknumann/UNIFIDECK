"""Per-game compatibility fixes — winetricks verbs, exe overrides, and global Proton defaults."""

from __future__ import annotations
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, cast
logger = logging.getLogger(__name__)
@dataclass(frozen=True)
class GameFix:
    """Per-game Proton compatibility fix.

    Attributes:
        winetricks: List of winetricks verbs to install in the prefix.
        exe_override: Relative path to a non-default executable
            inside the game install directory; overrides the
            Legendary/legendary-resolved exe.
        notes: Free-form human-readable explanation.
        source: Provenance — ``"manual"``, ``"umu-protonfixes"``
            or ``"global_default"``.
    """
    winetricks: list[str] = field(default_factory=list)
    exe_override: str | None = None
    notes: str = ""
    source: str = ""

GLOBAL_DEFAULTS: list[str] = [
    "vcrun2005",
    "vcrun2008",
    "vcrun2010",
    "vcrun2012",
    "vcrun2013",
    "vcrun2022",
    "d3dcompiler_47",
    "d3dcompiler_43",
    "mfc140",
]
MANUAL_FIXES: dict[str, GameFix] = {
    "Dodo": GameFix(
        winetricks=[],
        notes="Works with Proton + EOS only",
        source="manual",
    ),
    "ea8df71f923649a193ab1c1fded7e1b3": GameFix(
        winetricks=[
            "vcrun2005", "vcrun2008", "vcrun2010", "vcrun2012",
            "vcrun2013", "vcrun2022",
        ],
        exe_override=(
            "Ghostrunner/Binaries/Win64/"
            "Ghostrunner-Win64-Shipping.exe"
        ),
        notes=(
            "UE4 stub bypassed — launches shipping binary "
            "directly. The default Ghostrunner.exe is a 540KB "
            "launcher stub that probes VC++ runtime registry "
            "keys via MsiQueryProductState and shows a "
            "'Microsoft Visual C++ Runtime' error even when "
            "DLLs are present. Proton rewrites system.reg at "
            "launch time, making registry injection impossible."
        ),
        source="manual",
    ),
    "fa5aa7e6c28c4c94aeac239eee700d5f": GameFix(
        winetricks=[],
        notes="EOS overlay only, no redistributables needed",
        source="manual",
    ),
}
_UMU_DATABASE_URL_FORMATS = [
    "https://raw.githubusercontent.com/Open-Wine-Components/"
    "umu-database/main/umu-egs-{game_id}.json",
    "https://raw.githubusercontent.com/Open-Wine-Components/"
    "umu-database/main/umu-epic-{game_id}.json",
]
_UMU_CACHE: dict[str, tuple[float, dict | None]] = {}
_CACHE_TTL_SECONDS = 3600
def get_exe_override(game_id: str) -> str | None:
    """Return the exe override for one game from the MANUAL_FIXES table.

    Args:
        game_id: Per-store game identifier.

    Returns:
        Relative exe path string or ``None`` if no manual override.
    """
    fix = MANUAL_FIXES.get(game_id)
    if fix is None:
        return None
    return fix.exe_override

async def fetch_umu_protonfixes(game_id: str) -> dict | None:

    """Fetch a game's umu-database JSON entry from GitHub (10s timeout).

    Tries both ``umu-egs-`` and ``umu-epic-`` URL formats. Results
    are cached for one hour per ``game_id``. Returns the parsed
    JSON or ``None`` for any miss / network failure.

    Args:
        game_id: Per-store game identifier.

    Returns:
        The protonfixes dict, or ``None`` on miss / error.
    """
    now = time.monotonic()
    cached = _UMU_CACHE.get(game_id)
    if (
        cached is not None
        and now - cached[0] < _CACHE_TTL_SECONDS
    ):
        return cached[1]
    _UMU_CACHE[game_id] = (now, None)
    try:
        import aiohttp
    except ImportError:
        logger.info(
            "[game_fixes] aiohttp not available, skipping "
            "umu-database lookup for %s", game_id,
        )
        return None
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for url_format in _UMU_DATABASE_URL_FORMATS:
            url = url_format.format(game_id=game_id)
            data = await _try_umu_url(session, url)
            if data is not None:
                logger.info(
                    "[game_fixes] found umu-db "
                    "entry for %s", game_id,
                )
                _UMU_CACHE[game_id] = (now, data)
                return cast("dict[Any, Any] | None", data)
    logger.info(
        "[game_fixes] no umu-db entry for %s (expected "
        "for most games)", game_id,
    )
    return None
async def _try_umu_url(
    session: Any, url: str,
) -> dict | None:
    """GET one umu-database URL and parse the response as JSON.

    Args:
        session: aiohttp ``ClientSession``.
        url: URL to fetch.

    Returns:
        Parsed JSON dict, or ``None`` on non-200 or any error.
    """
    import aiohttp
    try:
        async with session.get(url) as response:
            if response.status != 200:
                return None
            data = await response.json(content_type=None)
            return cast("dict[Any, Any] | None", data)
    except (aiohttp.ClientError, json.JSONDecodeError) as e:
        logger.debug(
            "[game_fixes] %s lookup failed: %s", url, e,
        )
        return None
async def get_required_winetricks(game_id: str) -> list[str]:
    """Compute the winetricks verb list for one game.

    Resolution order: manual override → umu-database
    ``winetricks`` → ``GLOBAL_DEFAULTS``.

    Args:
        game_id: Per-store game identifier.

    Returns:
        List of winetricks verbs (copied from source list).
    """
    manual = MANUAL_FIXES.get(game_id)
    if manual is not None:
        logger.info(
            "[game_fixes] manual override for %s: %s",
            game_id, manual.winetricks,
        )
        return list(manual.winetricks)
    umu_data = await fetch_umu_protonfixes(game_id)
    if umu_data and isinstance(
        umu_data.get("winetricks"), list,
    ):
        packages = umu_data["winetricks"]
        logger.info(
            "[game_fixes] umu-db for %s: %s",
            game_id, packages,
        )
        return list(packages)
    logger.info(
        "[game_fixes] global defaults for %s", game_id,
    )
    return list(GLOBAL_DEFAULTS)

async def get_game_fix(game_id: str) -> GameFix:

    """Compute the full ``GameFix`` for one game.

    Resolution order: manual override → umu-database fix
    (winetricks + exe_override + notes) → global defaults.

    Args:
        game_id: Per-store game identifier.

    Returns:
        A ``GameFix`` with ``source`` indicating provenance.
    """
    manual = MANUAL_FIXES.get(game_id)
    if manual is not None:
        return manual
    umu_data = await fetch_umu_protonfixes(game_id)
    if umu_data:
        return GameFix(
            winetricks=list(
                umu_data.get("winetricks") or [],
            ),
            exe_override=umu_data.get("exe_override"),
            notes=str(umu_data.get("notes") or ""),
            source="umu-protonfixes",
        )
    return GameFix(
        winetricks=list(GLOBAL_DEFAULTS),
        notes=(
            "Using global defaults "
            "(vcrun*, d3dcompiler, mfc140)"
        ),
        source="global_default",
    )
def clear_cache() -> None:
    """Empty the in-memory umu-database cache.

    Primarily a test hook; production code does not call this.
    """
    _UMU_CACHE.clear()