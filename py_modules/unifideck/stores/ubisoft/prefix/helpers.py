"""
Wine prefix helpers — symlink fixups, marker writing, basic file ops.

OP-59b | py_modules/unifideck/stores/ubisoft/prefix/helpers.py

Helper class with a grab-bag of operations the prefix builders rely on:

* ``fix_pfx_symlink`` — fixes the legacy ``<prefix>/pfx`` symlink some
  Proton versions expect;
* ``write_bootstrap_marker`` — writes the marker file that flags a
  prefix as "Unifideck-managed";
* ``has_bootstrap_marker`` — checks a prefix for the marker;
* misc. ``Path``-based wrappers around create/delete/check operations.

Kept as a separate module so the builders can stay focused on the
high-level construction logic.
"""

from __future__ import annotations
import asyncio
import datetime
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .manager import UbisoftPrefixManager
logger = logging.getLogger(__name__)
_SILENT_INSTALL_FLAG = "/S"


class _PrefixHelpers:
    """Grab-bag helpers used by the prefix builders.

    Covers prefix cloning (rsync from template), fresh installs
    (via the cached UPC installer), background template seeding,
    the ``pfx`` symlink quirk, bootstrap marker writing, and
    auth-state injection wiring.
    """

    def __init__(self, parent: UbisoftPrefixManager) -> None:
        """Bind the prefix-helpers to their parent prefix manager.

        Args:
            parent: Owning ``UbisoftPrefixManager`` instance
                (provides config + paths + bootstrap marker).
        """
        self._parent = parent

    async def clone_prefix_from_template(
        self,
        space_id: str,
        prefix_path: str,
    ) -> bool:
        """Rsync the template prefix into a fresh per-game prefix.

        On success, writes the bootstrap marker and injects auth
        state.

        Args:
            space_id: UPC space_id (for marker and logs).
            prefix_path: Destination prefix directory.

        Returns:
            True iff the rsync succeeded.
        """
        logger.info(
            "[UbisoftPrefixManager] cloning template for %s",
            space_id,
        )
        try:
            os.makedirs(prefix_path, exist_ok=True)
            ok = await self.rsync_clone(
                self._parent._config.template_dir_expanded,
                prefix_path,
                exclude_games=False,
            )
            if not ok:
                logger.error(
                    "[UbisoftPrefixManager] rsync clone failed for %s",
                    space_id,
                )
                return False
            self.write_bootstrap_marker(
                prefix_path,
                "cloned_from_template",
                space_id,
            )
            self.try_inject_auth_state([prefix_path])
            logger.info(
                "[UbisoftPrefixManager] prefix cloned for %s",
                space_id,
            )
            return True
        except Exception as e:
            logger.error(
                "[UbisoftPrefixManager] clone failed: %s",
                e,
            )
            return False

    async def create_prefix_from_fresh_install(
        self,
        space_id: str,
        prefix_path: str,
    ) -> bool:
        """Build a per-game prefix by running the UPC installer fresh.

        Used as a fallback when no template is available. On success,
        writes the marker, injects auth state, and seeds the template
        from this prefix for future installs.

        Args:
            space_id: UPC space_id (for marker and logs).
            prefix_path: Destination prefix directory.

        Returns:
            True iff the installer ran successfully and produced upc.exe.
        """
        logger.info(
            "[UbisoftPrefixManager] fresh install for %s",
            space_id,
        )
        installer_path = await self._parent._installer_cache.ensure_cached()
        if not installer_path:
            return False
        try:
            os.makedirs(prefix_path, exist_ok=True)
            success = await self.run_silent_installer(
                prefix_dir=prefix_path,
                installer_path=installer_path,
                gameid=f"umu-ubisoft-{space_id}",
                store_game_id=f"ubisoft:{space_id}",
            )
            if not success:
                return False
            if not self._parent._paths.find_upc_exe(prefix_path):
                logger.error(
                    "[UbisoftPrefixManager] upc.exe not "
                    "found after fresh install for %s",
                    space_id,
                )
                return False
            self.write_bootstrap_marker(
                prefix_path,
                "fresh_install",
                space_id,
            )
            self.try_inject_auth_state([prefix_path])
            if not self._parent.template_exists():
                await self.create_template_from_game_prefix(
                    prefix_path,
                )
            return True
        except Exception as e:
            logger.exception(
                "[UbisoftPrefixManager] fresh install failed for %s: %s",
                space_id,
                e,
            )
            return False

    async def create_template_from_game_prefix(
        self,
        game_prefix: str,
    ) -> None:
        """Seed the template prefix by rsync-cloning an existing game prefix.

        Used after a fresh install when no template exists yet — much
        cheaper than re-running the UPC installer.

        Args:
            game_prefix: Source game prefix to clone from.
        """
        template_dir = self._parent._config.template_dir_expanded
        logger.info(
            "[UbisoftPrefixManager] creating template from first game prefix",
        )
        try:
            os.makedirs(template_dir, exist_ok=True)
            ok = await self.rsync_clone(
                game_prefix,
                template_dir,
                exclude_games=False,
            )
            if not ok:
                return
            self.write_bootstrap_marker(
                template_dir,
                "template",
                None,
            )
            self.try_inject_auth_state([template_dir])
        except Exception as e:
            logger.warning(
                "[UbisoftPrefixManager] template creation from game prefix failed: %s",
                e,
            )

    async def run_silent_installer(
        self,
        *,
        prefix_dir: str,
        installer_path: str,
        gameid: str,
        store_game_id: str | None = None,
    ) -> bool:
        """Run the UPC installer in /S unattended mode inside a prefix.

        Args:
            prefix_dir: Target Wine prefix.
            installer_path: Cached installer .exe.
            gameid: ``GAMEID`` env var for umu.
            store_game_id: Optional ``UMU_STORE_ID`` for telemetry.

        Returns:
            True iff the installer subprocess exited 0.
        """
        umu_run = self._parent._binaries.find_umu_run()
        if not umu_run:
            logger.error(
                "[UbisoftPrefixManager] umu-run not found",
            )
            return False
        env = self._parent._binaries.build_umu_env(
            wineprefix=prefix_dir,
            gameid=gameid,
            store_game_id=store_game_id,
        )
        python_bin = self._parent._binaries.find_python()
        logger.info(
            "[UbisoftPrefixManager] installer run: PROTONPATH=%s GAMEID=%s",
            env.get("PROTONPATH"),
            env.get("GAMEID"),
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                python_bin,
                umu_run,
                installer_path,
                _SILENT_INSTALL_FLAG,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            logger.error(
                "[UbisoftPrefixManager] subprocess spawn failed: %s",
                e,
            )
            return False
        return await self._await_installer_completion(proc)

    @staticmethod
    async def _await_installer_completion(
        proc: asyncio.subprocess.Process,
    ) -> bool:
        """Wait up to 15 min for the installer subprocess to exit.

        Times out and kills the process if it stalls. Logs the first
        500 bytes of stderr on non-zero exit.

        Args:
            proc: The installer subprocess.

        Returns:
            True iff exit code is 0.
        """
        try:
            _stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=15 * 60,
            )
        except TimeoutError:
            logger.error(
                "[UbisoftPrefixManager] installer timed out after 15 min — killing",
            )
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            return False
        if proc.returncode != 0:
            stderr_text = (
                stderr.decode(
                    errors="replace",
                )[:500]
                if stderr
                else ""
            )
            logger.error(
                "[UbisoftPrefixManager] installer exited %d: %s",
                proc.returncode,
                stderr_text,
            )
            return False
        return True

    async def rsync_clone(
        self,
        src: str,
        dst: str,
        *,
        exclude_games: bool,
    ) -> bool:
        """Recursively clone one prefix to another via ``rsync -a``.

        Args:
            src: Source directory.
            dst: Destination directory.
            exclude_games: When True, skips the ``games/`` subdir
                (template/auth use cases).

        Returns:
            True iff rsync exited 0 within the 30-minute timeout.
        """
        args: list[str] = ["rsync", "-a"]
        if exclude_games:
            args.append("--exclude=games")
        args.extend([f"{src}/", f"{dst}/"])
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            logger.error(
                "[UbisoftPrefixManager] rsync spawn failed: %s",
                e,
            )
            return False
        try:
            _stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=30 * 60,
            )
        except TimeoutError:
            logger.error(
                "[UbisoftPrefixManager] rsync timed out — killing",
            )
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            return False
        if proc.returncode != 0:
            logger.error(
                "[UbisoftPrefixManager] rsync failed (%d): %s",
                proc.returncode,
                stderr.decode(errors="replace")[:300],
            )
            return False
        return True

    @staticmethod
    def fix_pfx_symlink(prefix_dir: str) -> None:
        """Repair the ``<prefix>/pfx`` self-symlink some Proton versions expect.

        If ``pfx`` exists as a symlink but doesn't point to ``.`` or
        the prefix itself, replace it with a symlink to the prefix
        root (modern layout).

        Args:
            prefix_dir: Wine prefix directory.
        """
        pfx_link = os.path.join(prefix_dir, "pfx")
        if not os.path.islink(pfx_link):
            return
        try:
            current_target = os.readlink(pfx_link)
            if current_target in (prefix_dir, "."):
                return
            os.remove(pfx_link)
            os.symlink(prefix_dir, pfx_link)
            logger.info(
                "[UbisoftPrefixManager] fixed pfx symlink: %s → %s",
                current_target,
                prefix_dir,
            )
        except OSError as e:
            logger.warning(
                "[UbisoftPrefixManager] could not fix pfx symlink: %s",
                e,
            )

    def write_bootstrap_marker(
        self,
        prefix_dir: str,
        source: str,
        space_id: str | None,
    ) -> None:
        """Write the ``<config.bootstrap_marker>`` flag inside a prefix.

        The marker records the source (``cloned_from_template``,
        ``fresh_install``, ``template``, …), optional game id, and
        creation timestamp. Failures are logged but not raised.

        Args:
            prefix_dir: Wine prefix directory.
            source: Free-form source label.
            space_id: Optional game ID to embed (omit for template / auth).
        """
        marker_path = os.path.join(
            prefix_dir,
            self._parent._config.bootstrap_marker,
        )
        created_at = datetime.datetime.now().isoformat()
        lines = [source, f"created={created_at}"]
        if space_id:
            lines.insert(1, f"game={space_id}")
            try:
                with open(
                    marker_path,
                    "w",
                    encoding="utf-8",
                ) as f:
                    f.write("\n".join(lines) + "\n")
            except OSError as e:
                logger.warning(
                    "[UbisoftPrefixManager] could not write bootstrap marker: %s",
                    e,
                )

    def try_inject_auth_state(
        self,
        prefix_paths: list[str],
    ) -> None:
        """Best-effort dispatch to the auth-state injector.

        Args:
            prefix_paths: Prefix directories to inject into.
        """
        if not prefix_paths:
            return
        try:
            self._parent._inject_auth_state(prefix_paths)
        except Exception as e:
            logger.warning(
                "[UbisoftPrefixManager] auth state injection failed: %s",
                e,
            )
