"""Auth flow — opens the store login URL captured by the backend and polls until the auth file disappears."""

from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from ...core.types import Result
from ..types.context import LaunchContext
from ..types.errors import DependencyMissingError, GameNotFoundError
if TYPE_CHECKING:
    from ...auth.edge_browser import EdgeBrowser
logger = logging.getLogger(__name__)
_AUTH_URL_FILES = {
    "epic": "epic_auth_url.txt",
    "gog": "gog_auth_url.txt",
    "amazon": "amazon_auth_url.txt",
}
_AUTH_STORE_LABELS = {
    "epic": "Epic Games",
    "gog": "GOG",
    "amazon": "Amazon Games",
}
_MAX_AUTH_SECONDS = 600
def _read_config_int(key: str, default: int) -> int:
    """Cold-start ConfigManager read for an int key.

    Bypasses the launcher service's config to allow reads
    before the service graph is wired.

    Args:
        key: Dotted config key.
        default: Default returned if the key is missing.

    Returns:
        Resolved int value.
    """
    from ...utils.config_helpers import read_config_int_cold_start
    return read_config_int_cold_start(key, default)
def _read_auth_url(store: str) -> str:
    """Read and validate the per-store auth URL captured by the backend.

    The backend writes the OAuth login URL to
    ``~/.local/share/unifideck/<store>_auth_url.txt`` when
    the user requests a re-auth.

    Args:
        store: Store identifier (``"epic"``, ``"gog"``, ``"amazon"``).

    Returns:
        URL string.

    Raises:
        GameNotFoundError: Unknown store, missing file,
            unreadable file, or empty contents.
    """
    filename = _AUTH_URL_FILES.get(store)
    if filename is None:
        raise GameNotFoundError(
            f"Unknown auth store {store!r}",
            context={"store": store},
        )
    url_file = Path(
        f"~/.local/share/unifideck/{filename}",
    ).expanduser()
    if not url_file.is_file():
        raise GameNotFoundError(
            f"Auth URL file not found: {url_file}",
            context={"store": store, "file": str(url_file)},
        )
    try:
        url = url_file.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise GameNotFoundError(
            f"Cannot read auth URL file: {e}",
            context={"store": store, "file": str(url_file)},
        ) from e
    if not url:
        raise GameNotFoundError(
            f"Auth URL file is empty: {url_file}",
            context={"store": store},
        )
    return url

async def handle_store_auth(
 ctx: LaunchContext,
 edge_browser: EdgeBrowser,
) -> Result:

    """Drive the OAuth flow for a store through the Edge browser.

    Reads the URL captured by the backend, launches Edge against
    it, then waits up to ``launcher.auth_max_seconds`` for the
    browser process to exit (the backend closes Edge once auth
    completes).

    Args:
        ctx: Launch context with ``auth_store`` set.
        edge_browser: Edge wrapper.

    Returns:
        A ``Result`` — success unless Edge failed to launch.

    Raises:
        GameNotFoundError: ``auth_store`` unset, unknown, or
            the URL file is invalid.
        DependencyMissingError: Edge flatpak not installed.
    """
    store = ctx.auth_store
    if store is None:
        raise GameNotFoundError(
            "handle_store_auth called without auth_store set",
            context={"game_key": ctx.game_key},
        )
    label = _AUTH_STORE_LABELS.get(store, store.title)
    logger.info(
        "[launcher.auth] launching %s OAuth flow", label,
    )
    if not edge_browser.is_installed:
        raise DependencyMissingError(
            "Microsoft Edge flatpak required for OAuth",
            context={"store": store},
        )
    auth_url = _read_auth_url(store)
    logger.info(
        "[launcher.auth] %s auth URL resolved (%d chars)",
        label, len(auth_url),
    )
    started = edge_browser.launch_auth(auth_url)
    if not started:
        return Result(
            success=False,
            error="edge_auth_launch_failed",
            store=store,
        )
    await _wait_for_auth_end(edge_browser)
    logger.info(
        "[launcher.auth] %s auth browser closed", label,
    )
    return Result(success=True, store=store)
async def _wait_for_auth_end(edge_browser: EdgeBrowser) -> None:
    """Block until Edge exits or the auth timeout expires.

    Polls if no process handle is available.

    Args:
        edge_browser: Edge wrapper.
    """
    max_seconds = _read_config_int(
        "launcher.auth_max_seconds", _MAX_AUTH_SECONDS,
    )
    proc = edge_browser.process
    if proc is not None:
        loop = asyncio.get_event_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, proc.wait),
                timeout=max_seconds,
            )
        except TimeoutError:
            logger.warning(
                "[launcher.auth] auth flow reached %ds timeout",
                max_seconds,
            )
        return
    elapsed = 0.0
    while elapsed < max_seconds:
        await asyncio.sleep(5.0)
        elapsed += 5.0