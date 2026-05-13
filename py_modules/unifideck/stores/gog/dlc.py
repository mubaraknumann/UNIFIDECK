"""GOG DLC manager — list, install, fetch storefront URL for downloadable content.

OP-22-gog-dlc | py_modules/unifideck/stores/gog/dlc.py

DLCs in GOG's data model are full
``Product`` entities (each with its own id and
metadata) linked to a parent game. This module:

1. **Lists DLCs** — two-stage API call: fetch
   the parent product expanded with ``downloads``,
   then follow the ``expanded_all_products_url``
   to get full DLC details;
2. **Probes available languages** — runs
   ``gogdl info --platform windows`` to find out
   which languages the DLC supports;
3. **Installs DLCs** — uses ``gogdl repair``
   against the DLC id with the game's install
   path; repair flow handles "install missing
   files" semantics that install doesn't;
4. **Fetches store URLs** — resolves the
   ``product_card`` link for the storefront UI.

Repair mode for DLC install is intentional —
"install" mode in gogdl assumes a fresh
destination, but DLCs go *into* an existing game
install. Repair detects missing files (the DLC)
and downloads them without disturbing the base
game.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from ...core.types import Result
from .config import GOGConfig
from .http import fetch_json_get
from .tokens import GOGTokenManager

logger = logging.getLogger(__name__)

_LANGUAGE_FALLBACK = ["en-US"]
_LANG_PROBE_TIMEOUT_S = 30.0


class GOGDlcManager:
    """DLC ops on top of GOG tokens + gogdl binary.

    Deps injected at construction; the
    ``resolve_install_path`` callable points back
    at the library for DLC installs that need to
    target an existing game install location.
    """

    def __init__(
        self,
        config: GOGConfig,
        tokens: GOGTokenManager,
        gogdl_bin: str,
        locale_fn: Callable[[], str],
        resolve_install_path: Callable[[str], dict[str, str | None] | None]
    ) -> None:
        """Stash dependencies.

        Args:
            config: ``GOGConfig``.
            tokens: ``GOGTokenManager``.
            gogdl_bin: gogdl binary path.
            locale_fn: callable returning user's
                current locale.
            resolve_install_path: callable
                returning the game's install
                info dict.
        """
        self._config = config
        self._tokens = tokens
        self._gogdl_bin = gogdl_bin
        self._locale_fn = locale_fn
        self._resolve_install = resolve_install_path

    async def get_game_dlcs(self, game_id: str) -> list[dict[str, Any]]:
        """Fetch the DLC list for a game — try expanded URL, fallback to basic.

        Pipeline:

        1. Refresh tokens; fail → empty list;
        2. Fetch parent product with
           ``?expand=downloads``;
        3. Walk to ``dlcs.expanded_all_products_url``
           and fetch it — this returns a list
           of full DLC product dicts;
        4. Fallback: if expanded fetch fails or
           the URL doesn't exist, return the
           basic ``dlcs.products`` list (just
           ids and titles, no metadata).

        Args:
            game_id: parent product id.

        Returns:
            List of DLC dicts (full or basic),
            empty on auth/network failure.
        """
        if not await self._tokens.refresh_if_stale():
            logger.warning(
                "[GOGDlcManager] not authenticated for DLC fetch",
            )
            return []
        access = self._tokens.access_token
        if not access:
            return []
        product_url = (
            f"{self._config.api_gog_url}/products/{game_id}"
            f"?expand=downloads&locale=en-US"
        )
        product = await self._http_get_json(
            product_url,
            bearer=access,
        )
        if not isinstance(product, dict):
            return []
        dlcs_info = product.get("dlcs", {})
        if not isinstance(dlcs_info, dict) or not dlcs_info:
            logger.debug(
                "[GOGDlcManager] no DLCs for %s",
                game_id,
            )
            return []
        expanded_url = dlcs_info.get(
            "expanded_all_products_url",
        )
        if isinstance(expanded_url, str) and expanded_url:
            expanded = await self._http_get_json(
                expanded_url,
                bearer=access,
            )
            if isinstance(expanded, list):
                logger.info(
                    "[GOGDlcManager] found %d DLCs for %s",
                    len(expanded),
                    game_id,
                )
                return expanded
            logger.warning(
                "[GOGDlcManager] expanded DLC list malformed for %s",
                game_id,
            )
        basic_products = dlcs_info.get("products", [])
        if isinstance(basic_products, list):
            return basic_products
        return []

    async def get_available_languages(self, game_id: str) -> list[str]:
        """Probe ``gogdl info`` for available languages; fall back to en-US.

        gogdl info reports the supported
        language list for a product (game or
        DLC). On any failure (timeout, spawn
        error, non-zero exit, no languages
        field) we return ``["en-US"]`` so the
        caller has *something* usable.

        Args:
            game_id: product id (game or DLC).

        Returns:
            List of language codes (always at
            least one).
        """
        stdout = await self._spawn_lang_probe(game_id)
        if stdout is None:
            return list(_LANGUAGE_FALLBACK)
        languages = self._parse_languages_from_info(stdout)
        if languages:
            logger.info(
                "[GOGDlcManager] %s languages: %s",
                game_id,
                languages,
            )
            return languages
        logger.warning(
            "[GOGDlcManager] no languages in info output for %s",
            game_id,
        )
        return list(_LANGUAGE_FALLBACK)

    async def _spawn_lang_probe(self, game_id: str) -> bytes | None:
        """Run ``gogdl info --platform windows`` with 30s timeout.

        Uses the async context manager form for
        gogdl credentials (``gogdl_credentials()``)
        — automatically releases the credentials
        on context exit even if the probe times
        out or raises.

        Returns ``None`` on:

        * Token refresh fail;
        * Missing gogdl bin;
        * Spawn OSError;
        * Probe timeout;
        * Non-zero exit code.

        Args:
            game_id: product id.

        Returns:
            Raw stdout, or ``None``.
        """
        if not await self._tokens.refresh_if_stale():
            return None
        if not Path(self._gogdl_bin).is_file():
            return None
        cmd = [
            self._gogdl_bin,
            "--auth-config-path",
            self._config.auth_config_path,
            "info",
            "--platform",
            "windows",
            game_id,
        ]
        try:
            async with self._tokens.gogdl_credentials() as env:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout, _stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=_LANG_PROBE_TIMEOUT_S,
                )
        except TimeoutError:
            logger.warning(
                "[GOGDlcManager] language probe timed out for %s",
                game_id,
            )
            return None
        except OSError as e:
            logger.warning(
                "[GOGDlcManager] gogdl spawn failed: %s",
                e,
            )
            return None
        if proc.returncode != 0:
            return None
        return stdout

    @staticmethod
    def _parse_languages_from_info(stdout: bytes) -> list[str]:
        """Walk gogdl info's JSON-lines stdout looking for a ``languages`` array.

        First valid line containing a non-empty
        ``languages`` field wins. Returns
        empty list if none found.

        Args:
            stdout: raw output bytes.

        Returns:
            Languages list (possibly empty).
        """
        for raw_line in stdout.decode(
            errors="replace",
        ).splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            langs = data.get("languages")
            if isinstance(langs, list) and langs:
                result = [str(x) for x in langs if x]
                if result:
                    return result
        return []

    async def install_dlc(
        self,
        game_id: str,
        dlc_id: str,
        base_path: str | None = None,
        progress_cb: (Callable[[dict[str, Any]], Awaitable[None]] | None) = None
    ) -> Result:
        """Install a DLC into an existing game install (uses gogdl repair).

        Pipeline:

        1. Preflight (gogdl exists + tokens
           valid);
        2. Resolve base_path (caller-supplied,
           or look up the parent game's install
           path);
        3. Pick locale via injected callable;
        4. Spawn ``gogdl repair`` for the DLC id;
        5. Read stdout, forward progress lines;
        6. Finalize.

        ``progress_cb`` receives a dict per
        progress line — DLC progress events
        include the dlc_id so the UI can route
        them to the correct row.

        Args:
            game_id: parent game id (used for
                base-path resolution).
            dlc_id: DLC product id.
            base_path: optional override.
            progress_cb: optional progress
                callback.

        Returns:
            ``Result``.
        """
        failure = await self._dlc_preflight()
        if failure is not None:
            return failure
        resolved_base = self._dlc_resolve_base_path(
            game_id,
            base_path,
        )
        preferred_lang = self._locale_fn() or "en-US"
        logger.info(
            "[GOGDlcManager] installing DLC %s for game %s at %s (lang=%s)",
            dlc_id,
            game_id,
            resolved_base,
            preferred_lang,
        )
        proc = await self._dlc_spawn_gogdl(
            dlc_id,
            resolved_base,
            preferred_lang,
        )
        if proc is None:
            return Result(
                success=False,
                error="gogdl_spawn_failed",
            )
        await self._dlc_read_loop(proc, dlc_id, progress_cb)
        return await self._dlc_finalize(proc, dlc_id)

    async def _dlc_preflight(self) -> Result | None:
        """Validate gogdl bin + auth before DLC install.

        Two checks; returns failure ``Result`` on
        either, ``None`` on success.

        Returns:
            Failure ``Result`` or ``None``.
        """
        if not Path(self._gogdl_bin).is_file():
            return Result(
                success=False,
                error="gogdl_not_found",
            )
        if not await self._tokens.refresh_if_stale():
            return Result(
                success=False,
                error="not_authenticated",
            )
        return None

    def _dlc_resolve_base_path(self, game_id: str, base_path: str | None) -> str:
        """Pick install path for DLC: explicit > parent game's > default download.

        DLC repair needs to find the parent
        game's files; pointing it at the wrong
        path would either fail or attempt to
        install the entire game.

        Args:
            game_id: parent game id.
            base_path: optional override.

        Returns:
            Resolved base path.
        """
        if base_path:
            return base_path
        info = self._resolve_install(game_id)
        if info and isinstance(info.get("install_path"), str):
            return cast("str", info["install_path"])
        return str(
            Path(self._config.download_dir).expanduser(),
        )

    async def _dlc_spawn_gogdl(self, dlc_id: str, base_path: str, lang: str) -> asyncio.subprocess.Process | None:
        """Spawn ``gogdl repair`` for the DLC, attach cleanup hook.

        Repair mode is used (not install)
        because the parent game's install dir
        already exists; gogdl repair will
        detect the missing DLC files and
        download just those.

        Args:
            dlc_id: DLC product id.
            base_path: parent game's install
                root.
            lang: language code.

        Returns:
            ``Process`` or ``None`` on spawn
            error.
        """
        cmd = [
            self._gogdl_bin,
            "--auth-config-path",
            self._config.auth_config_path,
            "repair",
            dlc_id,
            "--platform",
            "windows",
            "--path",
            base_path,
            "--lang",
            lang,
        ]
        try:
            env, cleanup = await self._tokens.acquire_gogdl_creds()
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            proc._unifideck_gogdl_cleanup = cleanup
            return proc
        except OSError as e:
            logger.error(
                "[GOGDlcManager] gogdl spawn failed: %s",
                e,
            )
            return None

    async def _dlc_read_loop(
        self,
        proc: asyncio.subprocess.Process,
        dlc_id: str,
        progress_cb: (Callable[[dict[str, Any]], Awaitable[None]] | None)
    ) -> None:
        """Stream stdout from the DLC subprocess, forward Progress: lines.

        Only ``Progress:`` lines are forwarded to
        the callback — other output isn't
        normalised for DLC display. Non-progress
        lines aren't even logged here (would
        clutter logs during multi-DLC bulk
        install).

        Args:
            proc: subprocess.
            dlc_id: DLC id (for progress
                payload).
            progress_cb: optional callback.
        """
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            line_str = line.decode(errors="replace").strip()
            if not line_str:
                continue
            if progress_cb is not None and "Progress:" in line_str:
                await self._forward_dlc_progress(
                    line_str,
                    dlc_id,
                    progress_cb,
                )

    async def _dlc_finalize(self, proc: asyncio.subprocess.Process, dlc_id: str) -> Result:
        """Wait for the DLC subprocess + check return code → ``Result``.

        Non-zero exit → return code surfaced in
        the error string for diagnostics.

        Args:
            proc: subprocess.
            dlc_id: DLC id.

        Returns:
            ``Result``.
        """
        await proc.wait()
        if proc.returncode != 0:
            logger.error(
                "[GOGDlcManager] DLC install failed (code %d)",
                proc.returncode,
            )
            return Result(
                success=False,
                error=(f"dlc_install_failed_code_{proc.returncode}"),
            )
        logger.info(
            "[GOGDlcManager] DLC %s installed successfully",
            dlc_id,
        )
        return Result(success=True)

    @staticmethod
    async def _forward_dlc_progress(line_str: str, dlc_id: str, progress_cb: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """Parse a Progress: line + invoke the callback with a normalised payload.

        Payload shape:
        ``{progress_percent, phase_message,
        dlc_id}``. The dlc_id lets the UI
        route progress events to the correct
        row when multiple DLCs are installing
        concurrently.

        Parse failures (ValueError, IndexError)
        log at DEBUG and skip — same approach as
        the install progress monitor.

        Args:
            line_str: stdout line.
            dlc_id: DLC product id.
            progress_cb: callback.
        """
        try:
            part = line_str.split("Progress:", 1)[1].strip()
            tokens = part.split()
            if not tokens:
                return
            percent = float(tokens[0])
            await progress_cb(
                {
                    "progress_percent": percent,
                    "phase_message": (f"Installing DLC… {percent:.1f}%"),
                    "dlc_id": dlc_id,
                }
            )
        except (ValueError, IndexError) as e:
            logger.debug(
                "[GOGDlcManager] DLC progress parse: %s",
                e,
            )

    async def get_game_store_url(self, game_id: str) -> str | None:
        """Fetch the GOG storefront URL for a game (used for "View on Store" links).

        Returns the ``product_card`` link from
        the products endpoint, expanded with
        ``description`` so we get the metadata
        + the link in one round trip.

        Args:
            game_id: product id.

        Returns:
            Storefront URL or ``None``.
        """
        url = f"{self._config.api_gog_url}/products/{game_id}?expand=description"
        data = await self._http_get_json(url)
        if not isinstance(data, dict):
            return None
        links = data.get("links", {})
        if not isinstance(links, dict):
            return None
        product_card = links.get("product_card")
        if isinstance(product_card, str) and product_card:
            return product_card
        return None

    async def _http_get_json(self, url: str, bearer: str | None = None) -> asyncio.subprocess.Process | None:
        """Thin wrapper around ``fetch_json_get`` with DLC defaults.

        10-second timeout, log prefix
        ``[GOGDlcManager]``. ``bearer`` is
        optional — store URL lookup doesn't
        require auth but DLC list does.

        Args:
            url: target URL.
            bearer: optional access token.

        Returns:
            Parsed JSON or ``None``.
        """
        return await fetch_json_get(url, bearer=bearer, user_agent=self._config.user_agent, timeout=10.0, log_prefix="[GOGDlcManager]")
