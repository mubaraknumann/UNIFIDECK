"""Post-install: locate the install, write a marker file, regenerate manifests.

OP-22-gog-install-marker
File: py_modules/unifideck/stores/gog/install/marker.py

After ``gogdl install`` returns, three things
need to happen:

1. **Locate the install** — gogdl may have
   installed into the predicted folder, or laid
   files flat in the base path (when the game has
   no specific folder name), or used a name
   different from what we predicted. This module
   handles all three cases.

2. **Write a marker file** — ``.unifideck-id``
   inside the install folder; contains the
   ``goggame-<id>.info`` data plus the chosen
   language. Lets the launcher know later which
   game lives where.

3. **Regenerate the manifest** — a final
   ``gogdl info`` after install so the cached
   manifest reflects the installed version
   (gogdl uses this for update checks).

The "flat install" reorganisation is the
fiddly case: when gogdl drops files directly into
``base_path`` instead of a subfolder, we have to
detect that (look for ``goggame-<id>.info`` at the
flat level) and move everything new into a
subfolder so the layout matches what the launcher
+ uninstaller expect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from typing import TYPE_CHECKING, Any, cast
from .primitives import GOGFolderOps

if TYPE_CHECKING:
    from .installer import GOGInstaller

logger = logging.getLogger(__name__)


class _PostInstallMarker:
    """Post-install orchestration — locate, mark, regen.

    Held by ``GOGInstaller`` as
    ``self._marker``. Back-reference to parent
    used for ``_gogdl_bin``, ``_config``,
    ``_tokens`` access during manifest
    regeneration.
    """

    def __init__(self, parent: GOGInstaller) -> None:
        """Stash parent reference.

        Args:
            parent: ``GOGInstaller``.
        """
        self._parent = parent

    @staticmethod
    def snapshot_dirs(base_path: str) -> set:
        """Take a snapshot of the directory listing — used as a pre-install baseline.

        Compared against the post-install
        listing in ``locate_install`` to identify
        *new* dirs created by gogdl.

        OSError → empty set; caller treats this
        as "no baseline" and falls back to other
        location heuristics.

        Args:
            base_path: directory to snapshot.

        Returns:
            Set of entry names.
        """
        try:
            return set(os.listdir(base_path))
        except OSError:
            return set()

    async def locate_install(self, game_id: str, base_path: str, folder_name: str | None, existing_dirs: set) -> str | None:
        """Find where gogdl actually installed the game.

        Three-stage search:

        1. **Flat install detection** — look for
           ``goggame-<id>.info`` directly in
           ``base_path``; if present, reorganise
           into a subfolder + return its path;
        2. **Predicted folder** — if
           ``folder_name`` exists as a subfolder
           of ``base_path``, return that path;
        3. **Scan for new dirs** — diff against
           ``existing_dirs`` to find newly-created
           subfolders; for each, check both
           ``./`` and ``./game/`` for the goggame
           marker.

        Return ``None`` if all three fail (caller
        will treat this as install failure).

        Args:
            game_id: GOG product id.
            base_path: install root.
            folder_name: predicted folder.
            existing_dirs: pre-install snapshot.

        Returns:
            Absolute path to the install, or
            ``None``.
        """
        flat_info = self._find_flat_goggame(base_path, game_id)
        if flat_info:
            return await self._reorganise_flat_install(
                base_path,
                game_id,
                folder_name,
                existing_dirs,
            )
        if folder_name:
            candidate = os.path.join(base_path, folder_name)
            if os.path.isdir(candidate):
                logger.info(
                    "[GOGInstaller] found at predicted: %s",
                    candidate,
                )
                return candidate
        try:
            current = set(os.listdir(base_path))
        except OSError:
            return None
        new_dirs = current - existing_dirs
        for name in new_dirs:
            item_path = os.path.join(base_path, name)
            if not os.path.isdir(item_path):
                continue
            for search_dir in (
                item_path,
                os.path.join(item_path, "game"),
            ):
                if GOGFolderOps.has_goggame_info(
                    search_dir,
                    game_id,
                ):
                    logger.info(
                        "[GOGInstaller] found via scan: %s",
                        item_path,
                    )
                    return item_path
        return None

    @staticmethod
    def _find_flat_goggame(base_path: str, game_id: str) -> bool:
        """Detect a flat install — ``goggame-<id>.info`` directly in ``base_path``.

        Walks ``base_path`` looking for the exact
        info file name. Doesn't recurse — flat is
        flat. OSError → False.

        Args:
            base_path: directory.
            game_id: product id.

        Returns:
            True iff the flat info file exists.
        """
        try:
            for name in os.listdir(base_path):
                full = os.path.join(base_path, name)
                if not os.path.isfile(full):
                    continue
                if name == f"goggame-{game_id}.info":
                    return True
        except OSError:
            pass
        return False

    @staticmethod
    async def _reorganise_flat_install(base_path: str, game_id: str, folder_name: str | None, existing_dirs: set) -> str:
        """Move flat-install files into a proper subfolder.

        Target folder = ``folder_name`` (preferred)
        or ``GOG_<id>``. Pipeline:

        1. mkdir target;
        2. Compute the set of new entries (current
           - existing);
        3. ``shutil.move`` each new entry into the
           target (skipping the target itself, in
           case it counts as new).

        Per-move errors are logged but don't
        abort — the install layout will be
        partial but recoverable.

        Args:
            base_path: install root.
            game_id: product id.
            folder_name: gogdl-reported folder
                name.
            existing_dirs: pre-install snapshot.

        Returns:
            Final target path.
        """
        target = folder_name or f"GOG_{game_id}"
        target_path = os.path.join(base_path, target)

        def _sync_move() -> None:
            """Blocking mkdir + move loop, runs in worker thread.

            Skips ``target`` itself in the diff so
            we don't try to move the destination
            folder into itself if it already
            existed in the snapshot.
            """
            os.makedirs(target_path, exist_ok=True)
            try:
                current = set(os.listdir(base_path))
            except OSError:
                return
            new_files = current - existing_dirs
            for item in new_files:
                if item == target:
                    continue
                src = os.path.join(base_path, item)
                dst = os.path.join(target_path, item)
                try:
                    shutil.move(src, dst)
                except OSError as e:
                    logger.warning(
                        "[GOGInstaller] move %s failed: %s",
                        item,
                        e,
                    )

        await asyncio.to_thread(_sync_move)
        logger.info(
            "[GOGInstaller] reorganised flat install → %s",
            target_path,
        )
        return target_path

    async def write_install_marker(self, install_path: str, game_id: str, language: str) -> bool:
        """Write the ``.unifideck-id`` marker file with goggame info + chosen language.

        The marker file gives the launcher
        everything it needs to recognise the
        install at runtime — game_id, language,
        plus the contents of the
        ``goggame-<id>.info`` file (if loadable).

        Args:
            install_path: install root.
            game_id: product id.
            language: chosen install language.

        Returns:
            True on successful write.
        """
        marker_path = os.path.join(install_path, ".unifideck-id")
        info_data = self._load_info_data_from_goggame(
            install_path,
            game_id,
        )
        info_data["language"] = language
        ok = await asyncio.to_thread(
            self._write_marker_sync,
            marker_path,
            info_data,
        )
        if ok:
            logger.info(
                "[GOGInstaller] wrote marker at %s (lang=%s)",
                marker_path,
                language,
            )
        return ok

    @staticmethod
    def _load_info_data_from_goggame(install_path: str, game_id: str) -> dict[str, Any]:
        """Load the ``goggame-<id>.info`` JSON; fallback to a stub if missing.

        Tries both ``install_path/`` and
        ``install_path/game/`` (some GOG games
        nest their install under a ``game/``
        subdirectory). Stops on first match that
        contains a ``name`` field; falls back to
        a stub dict with just ``game_id`` if all
        loads fail.

        Args:
            install_path: install root.
            game_id: product id.

        Returns:
            Info dict (always has ``game_id``).
        """
        info_data: dict[str, Any] = {"game_id": game_id}
        for candidate in (
            install_path,
            os.path.join(install_path, "game"),
        ):
            if not os.path.isdir(candidate):
                continue
            loaded = _PostInstallMarker._try_load_info_in_dir(
                candidate,
                game_id,
            )
            if loaded is not None:
                info_data = loaded
                if "name" in info_data:
                    break
        return info_data

    @staticmethod
    def _try_load_info_in_dir(directory: str, game_id: str) -> dict[str, Any] | None:
        """Read the first ``goggame-*.info`` file in ``directory`` and parse as JSON.

        Returns ``None`` on:

        * ``OSError`` listing the directory;
        * ``OSError`` reading the file;
        * JSON parse error;
        * Empty directory (no goggame files).

        On success, adds ``game_id`` to the parsed
        dict (gogdl's info file uses
        ``"gameId"`` rather than ``"game_id"``,
        we standardise on ``"game_id"``).

        Args:
            directory: directory to scan.
            game_id: product id (added to result).

        Returns:
            Parsed dict, or ``None``.
        """
        try:
            entries = os.listdir(directory)
        except OSError:
            return None
        for name in entries:
            if not name.startswith("goggame-"):
                continue
            if not name.endswith(".info"):
                continue
            try:
                with open(
                    os.path.join(directory, name),
                    encoding="utf-8",
                ) as f:
                    parsed = json.load(f)
                    parsed["game_id"] = game_id
                    return cast("dict[str, Any] | None", parsed)
            except (OSError, json.JSONDecodeError):
                return None
        return None

    @staticmethod
    def _write_marker_sync(marker_path: str, info_data: dict[str, Any]) -> bool:
        """Blocking JSON write of the marker file (UTF-8, indented).

        Indent=2 for human readability — the
        marker is small (KB-range), the
        indentation overhead is negligible.

        Args:
            marker_path: target file.
            info_data: payload.

        Returns:
            True on success.
        """
        try:
            with open(marker_path, "w", encoding="utf-8") as f:
                json.dump(info_data, f, indent=2)
            return True
        except OSError as e:
            logger.error(
                "[GOGInstaller] marker write failed: %s",
                e,
            )
            return False

    async def regenerate_manifest(self, game_id: str, platform: str) -> None:
        """Re-run ``gogdl info`` after install so the cached manifest is current.

        Without this step, the gogdl cache may
        still report the *pre-install* manifest,
        which confuses update checks (``check_for_updates``
        thinks the game is already at the latest
        version because the cache shows the
        version we just installed).

        Errors are non-fatal — the install
        succeeded, this is just a cache refresh.
        Logs at INFO with the exit code so
        post-mortems are possible.

        Args:
            game_id: product id.
            platform: ``"linux"`` or ``"windows"``.
        """
        cmd = [self._parent._gogdl_bin, "--auth-config-path", self._parent._config.auth_config_path, "info", "--platform", platform, game_id]
        try:
            env, _gogdl_cleanup = await self._parent._tokens.acquire_gogdl_creds()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                await proc.wait()
                logger.info(
                    "[GOGInstaller] manifest regenerated (code %d)",
                    proc.returncode,
                )
            finally:
                await _gogdl_cleanup()
        except OSError as e:
            logger.error(
                "[GOGInstaller] manifest regen failed: %s",
                e,
            )
