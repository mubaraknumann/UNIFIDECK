"""GOG update checker + update runner — buildId comparison + gogdl update subprocess.

OP-22-gog-updates | py_modules/unifideck/stores/gog/updates.py

Update logic uses GOG's content-system endpoint
(``content-system.gog.com``) which returns the
list of available builds for a product. The first
build in the list is the latest; comparing its
``build_id`` to the locally-installed buildId
tells us if an update is available.

Local buildId sources (in priority order):

1. ``goggame-<id>.info`` — gogdl writes it
   post-install;
2. ``.unifideck-id`` marker — fallback if
   goggame info is missing.

Update execution mirrors the install pipeline but
uses ``gogdl update`` instead of ``install``.
We don't do progress reporting for updates (the
UI just shows "updating…") — output is logged at
INFO for diagnostics.

The check uses Windows platform builds always —
GOG's API is reliable for windows; Linux builds
sometimes return stale data.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from ...core.types import Result
from .config import GOGConfig
from .http import fetch_json_get
from .tokens import GOGTokenManager

logger = logging.getLogger(__name__)

_CONTENT_SYSTEM_URL_TEMPLATE = "https://content-system.gog.com/products/{game_id}/os/windows/builds?generation=2"
_UPDATE_CHECK_TIMEOUT_S = 10.0


class GOGUpdatesChecker:
    """Update detection + execution for GOG installs.

    Deps (config, tokens, gogdl bin) injected at
    construction; the two callables
    (``get_installed_ids``,
    ``resolve_install_info``) point back at the
    library so we don't tightly couple update
    checking to library iteration.
    """

    def __init__(
        self,
        config: GOGConfig,
        tokens: GOGTokenManager,
        gogdl_bin: str,
        get_installed_ids: Callable[[], list[str]],
        resolve_install_info: Callable[[str], dict[str, str | None] | None]
    ) -> None:
        """Stash dependencies.

        Args:
            config: ``GOGConfig``.
            tokens: ``GOGTokenManager``.
            gogdl_bin: gogdl binary path.
            get_installed_ids: callable returning
                the list of installed game ids.
            resolve_install_info: callable that
                takes a game id and returns its
                ``{install_path}`` dict, or
                ``None`` if not installed.
        """
        self._config = config
        self._tokens = tokens
        self._gogdl_bin = gogdl_bin
        self._get_installed = get_installed_ids
        self._resolve_info = resolve_install_info

    @staticmethod
    def get_local_build_id(install_path: str, game_id: str) -> str | None:
        """Find the locally-installed buildId from goggame info or marker file.

        Priority:

        1. ``goggame-<id>.info`` in
           ``install_path/`` or
           ``install_path/game/`` — read the
           ``buildId`` field;
        2. ``.unifideck-id`` marker as fallback
           (post-Sprint-12 markers include
           ``buildId`` since they cache the
           goggame info).

        Returns ``None`` if no buildId is found —
        caller treats this as "can't check"
        rather than "no update".

        Args:
            install_path: install root.
            game_id: product id.

        Returns:
            Local buildId or ``None``.
        """
        install_p = Path(install_path)
        for search_dir in (install_p, install_p / "game"):
            info_file = search_dir / f"goggame-{game_id}.info"
            if not info_file.is_file():
                continue
            try:
                data = json.loads(
                    info_file.read_text(encoding="utf-8"),
                )
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(
                    "[GOGUpdatesChecker] info read failed for %s: %s",
                    info_file,
                    e,
                )
                continue
            build_id = data.get("buildId")
            if build_id:
                logger.debug(
                    "[GOGUpdatesChecker] local buildId for %s: %s",
                    game_id,
                    build_id,
                )
                return str(build_id)
        marker_path = install_p / ".unifideck-id"
        if marker_path.is_file():
            try:
                data = json.loads(
                    marker_path.read_text(
                        encoding="utf-8",
                    ).strip(),
                )
            except (OSError, json.JSONDecodeError):
                return None
            if isinstance(data, dict):
                build_id = data.get("buildId")
                if build_id:
                    logger.debug(
                        "[GOGUpdatesChecker] marker buildId for %s: %s",
                        game_id,
                        build_id,
                    )
                    return str(build_id)
        return None

    async def check_for_game_update(self, game_id: str) -> bool | None:
        """Check whether a single game has an available update.

        Pipeline:

        1. Refresh tokens; fail → return
           ``None`` (can't auth, can't check);
        2. Resolve install path; not installed
           → return ``None`` (nothing to update);
        3. Read local buildId; missing →
           ``None`` (can't compare);
        4. Fetch remote latest buildId;
        5. Compare — return True iff different.

        Note ``None`` vs ``False`` distinction:
        ``None`` means "couldn't determine",
        ``False`` means "definitively no update".

        Args:
            game_id: product id.

        Returns:
            True if update available, False if
            up-to-date, None if can't check.
        """
        if not await self._tokens.refresh_if_stale():
            logger.warning(
                "[GOGUpdatesChecker] not authenticated for update check of %s",
                game_id,
            )
            return None
        info = self._resolve_info(game_id)
        install_path = info.get("install_path") if info else None
        if not install_path:
            logger.debug(
                "[GOGUpdatesChecker] %s not installed, skipping",
                game_id,
            )
            return None
        local_build_id = self.get_local_build_id(
            install_path,
            game_id,
        )
        if not local_build_id:
            logger.warning(
                "[GOGUpdatesChecker] no local buildId for %s — cannot check for update",
                game_id,
            )
            return None
        remote_build_id = await self._fetch_remote_build_id(
            game_id,
        )
        if remote_build_id is None:
            return None
        logger.info(
            "[GOGUpdatesChecker] %s: local=%s, remote=%s",
            game_id,
            local_build_id,
            remote_build_id,
        )
        has_update = remote_build_id != local_build_id
        if has_update:
            logger.info(
                "[GOGUpdatesChecker] update available for %s",
                game_id,
            )
        return has_update

    async def _fetch_remote_build_id(self, game_id: str) -> str | None:
        """GET the content-system endpoint, return the latest build_id.

        Response shape:
        ``{items: [{build_id: ..., ...}, ...]}``.
        First entry is the latest build (GOG
        sorts most-recent-first).

        Returns ``None`` on any failure (no token,
        non-200, malformed response). Caller
        treats ``None`` as "can't check, assume
        up-to-date" to avoid spamming users.

        Args:
            game_id: product id.

        Returns:
            Remote buildId or ``None``.
        """
        access = self._tokens.access_token
        if not access:
            return None
        url = _CONTENT_SYSTEM_URL_TEMPLATE.format(
            game_id=game_id,
        )
        data = await fetch_json_get(
            url,
            bearer=access,
            user_agent=self._config.user_agent,
            timeout=_UPDATE_CHECK_TIMEOUT_S,
            log_prefix=f"[GOGUpdatesChecker] {game_id}",
        )
        if not isinstance(data, dict):
            return None
        items = data.get("items")
        if not isinstance(items, list) or not items:
            logger.warning(
                "[GOGUpdatesChecker] no builds returned for %s",
                game_id,
            )
            return None
        first = items[0]
        if not isinstance(first, dict):
            return None
        build_id = first.get("build_id")
        if build_id is None:
            return None
        return str(build_id)

    async def check_for_updates(self) -> list[str]:
        """Bulk update check across all installed GOG games.

        Iterates the installed list, calling
        ``check_for_game_update`` for each.
        Returns just the ids that have updates;
        ``None`` results (couldn't check) are
        treated as "no update" for the purposes
        of this list — UI will refresh on next
        check.

        Logs a summary at INFO so we can see
        update-check pressure in logs.

        Returns:
            List of game ids with available
            updates.
        """
        installed_ids = self._get_installed()
        if not installed_ids:
            return []
        updates: list[str] = []
        for game_id in installed_ids:
            has_update = await self.check_for_game_update(
                game_id,
            )
            if has_update:
                updates.append(game_id)
        logger.info(
            "[GOGUpdatesChecker] bulk check: %d/%d have updates",
            len(updates),
            len(installed_ids),
        )
        return updates

    async def update_game(self, game_id: str, install_path: str | None = None) -> Result:
        """Run ``gogdl update`` for a game.

        Pipeline:

        1. Verify gogdl bin exists;
        2. Resolve the install path (caller-
           provided or from
           ``_resolve_info``);
        3. Refresh tokens;
        4. Spawn gogdl update;
        5. Drain stdout for diagnostics;
        6. Finalize: check return code,
           return Result.

        Always uses ``--platform windows`` —
        GOG's update flow for non-native games
        is more stable on the windows track.

        Args:
            game_id: product id.
            install_path: optional override.

        Returns:
            ``Result``.
        """
        gogdl_exists = await asyncio.to_thread(
            Path(self._gogdl_bin).is_file,
        )
        if not gogdl_exists:
            return Result(
                success=False,
                error="gogdl_not_found",
            )
        resolved_path, path_failure = self._update_resolve_path(
            game_id,
            install_path,
        )
        if path_failure is not None:
            return cast("Result", path_failure)
        if not await self._tokens.refresh_if_stale():
            return Result(
                success=False,
                error="not_authenticated",
            )
        logger.info(
            "[GOGUpdatesChecker] starting update for %s at %s",
            game_id,
            resolved_path,
        )
        proc = await self._update_spawn_gogdl(
            game_id,
            resolved_path,
        )
        if proc is None:
            return Result(
                success=False,
                error="gogdl_spawn_failed",
            )
        await self._update_drain_output(proc)
        return await self._update_finalize(proc, game_id)

    def _update_resolve_path(self, game_id: str, install_path: str | None) -> tuple:
        """Resolve install path — explicit arg wins over library lookup.

        Returns ``(path, None)`` on success or
        ``(None, failure_result)`` on
        unresolvable.

        Args:
            game_id: product id.
            install_path: optional override.

        Returns:
            ``(path_or_None, result_or_None)``.
        """
        if install_path:
            return install_path, None
        info = self._resolve_info(game_id)
        if info and isinstance(info.get("install_path"), str):
            return info["install_path"], None
        return None, Result(
            success=False,
            error="install_path_not_found",
        )

    async def _update_spawn_gogdl(self, game_id: str, install_path: str) -> Any | None:
        """Spawn the gogdl update subprocess + attach cleanup hook.

        Same pattern as the install spawn —
        ``_unifideck_gogdl_cleanup`` attribute
        attached to the proc so the drain loop's
        finally can release it.

        OSError on spawn → log + return None.

        Args:
            game_id: product id.
            install_path: install root.

        Returns:
            ``Process`` or ``None``.
        """
        cmd = [
            self._gogdl_bin,
            "--auth-config-path",
            self._config.auth_config_path,
            "update",
            game_id,
            "--path",
            install_path,
            "--platform",
            "windows",
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
                "[GOGUpdatesChecker] gogdl spawn failed: %s",
                e,
            )
            return None

    @staticmethod
    async def _update_drain_output(proc: Any) -> None:
        """Read all stdout lines from the update subprocess at INFO log level.

        No stall timeout (updates can legitimately
        sit idle while gogdl re-uses cached
        chunks). Done when stdout EOFs.

        Args:
            proc: subprocess.
        """
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            line_str = line.decode(errors="replace").strip()
            if line_str:
                logger.info(
                    "[GOGUpdatesChecker/update] %s",
                    line_str,
                )

    @staticmethod
    async def _update_finalize(proc: Any, game_id: str) -> Result:
        """Wait for the update process, check return code, return ``Result``.

        Non-zero exit → return code surfaced in
        the error string for easier diagnosis.

        Args:
            proc: subprocess.
            game_id: product id.

        Returns:
            ``Result``.
        """
        await proc.wait()
        if proc.returncode != 0:
            logger.error(
                "[GOGUpdatesChecker] update failed for %s (code %d)",
                game_id,
                proc.returncode,
            )
            return Result(
                success=False,
                error=f"update_failed_code_{proc.returncode}",
            )
        logger.info(
            "[GOGUpdatesChecker] successfully updated %s",
            game_id,
        )
        return Result(success=True)
