"""Install-mode planner — decide download vs repair, verify completeness.

OP-22-gog-install-planner
File: py_modules/unifideck/stores/gog/install/planner.py

Before launching gogdl, two questions need
answering:

1. **What mode?** Fresh download, repair existing
   install, or cleanup + redownload (orphaned or
   corrupt data)?
2. **Did the install complete cleanly?** After
   gogdl returns success, we still want to check
   the install dir has the expected size + the
   goggame info file + a launchable exe.

The mode-selection logic uses two thresholds:

* ``_CORRUPT_INSTALL_SIZE_THRESHOLD`` (100MB) —
  if a folder has the goggame info but only a few
  MB on disk, gogdl was interrupted mid-install
  → wipe + redownload;
* ``_MIN_SIZE_RATIO`` (0.8) — post-install, if
  actual size is under 80% of expected, treat
  as incomplete.

Both thresholds are heuristics: gogdl doesn't
expose a "this install is incomplete" flag, so we
infer from observable state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import GOGConfig
from ..tokens import GOGTokenManager
from .primitives import GOGFolderOps

logger = logging.getLogger(__name__)

_CORRUPT_INSTALL_SIZE_THRESHOLD = 100 * 1024 * 1024
_MIN_SIZE_RATIO = 0.8


def _extract_disk_size_from_size_info(size_info: dict) -> int | None:
    """Pull the disk_size from a gogdl size-info dict, preferring English keys.

    gogdl's ``size`` field maps language code →
    ``{disk_size, download_size}``. We prefer
    English (``en-US``, ``en``, ``"*"``) because
    that's the most common base download for
    sizing purposes. Falls back to the first
    available key.

    Args:
        size_info: ``{lang: {disk_size: int}}``
            dict from gogdl.

    Returns:
        Disk size in bytes, or ``None`` if dict
        is empty.
    """
    for lang_key in ("en-US", "en", "*"):
        if lang_key in size_info:
            return int(
                size_info[lang_key].get("disk_size", 0) or 0,
            )
    if size_info:
        first = next(iter(size_info))
        return int(
            size_info[first].get("disk_size", 0) or 0,
        )
    return None


class GOGInstallPlanner:
    """Decide install mode + verify post-install state.

    Stateless from one call to the next — holds
    only config + tokens; planner state lives
    in the returned mode strings.

    The ``_gogdl_bin_override`` attribute is
    set by ``set_gogdl_bin`` — kept dynamic so
    the installer can pass it without
    constructor coupling.
    """

    def __init__(self, config: GOGConfig, tokens: GOGTokenManager) -> None:
        """Stash injected dependencies.

        Args:
            config: ``GOGConfig``.
            tokens: ``GOGTokenManager``.
        """
        self._config = config
        self._tokens = tokens

    async def determine_install_mode(self, game_id: str, target_folder: str | None) -> str:
        """Inspect the target folder and decide ``"download"`` vs ``"repair"``.

        Decision tree:

        1. No folder / doesn't exist →
           ``"download"``;
        2. Has goggame info + size >= 100MB →
           ``"repair"`` (probably a complete
           install, gogdl will fix any corruption);
        3. Has goggame info but size < 100MB →
           cleanup + ``"download"`` (interrupted
           install);
        4. No goggame info but data present →
           cleanup + ``"download"`` (orphaned
           data, e.g. leftover from a wrong
           uninstall).

        Args:
            game_id: product id.
            target_folder: predicted folder path
                (may be ``None``).

        Returns:
            ``"download"`` or ``"repair"``.
        """
        if not target_folder or not Path(target_folder).exists():
            logger.info(
                "[GOGInstallPlanner] folder missing → download",
            )
            return "download"
        folder_size = GOGFolderOps.folder_size(target_folder)
        file_count = GOGFolderOps.count_files(target_folder)
        has_info = GOGFolderOps.has_goggame_info(
            target_folder,
            game_id,
        )
        logger.info(
            "[GOGInstallPlanner] folder state: size=%.1fMB, files=%d, has_info=%s",
            folder_size / (1024 * 1024),
            file_count,
            has_info,
        )
        if has_info:
            if folder_size < _CORRUPT_INSTALL_SIZE_THRESHOLD:
                logger.warning(
                    "[GOGInstallPlanner] corrupt install "
                    "(has info but only %.1fMB) → cleanup "
                    "+ download",
                    folder_size / (1024 * 1024),
                )
                await self._cleanup_corrupt_install(
                    game_id,
                    target_folder,
                )
                return "download"
            logger.info(
                "[GOGInstallPlanner] valid existing install → repair",
            )
            return "repair"
        if folder_size > _CORRUPT_INSTALL_SIZE_THRESHOLD or file_count > 0:
            logger.warning(
                "[GOGInstallPlanner] orphaned data (no info, "
                "%.1fMB) → cleanup + download",
                folder_size / (1024 * 1024),
            )
            await self._cleanup_orphaned_install(
                game_id,
                target_folder,
            )
        return "download"

    async def verify_installation(self, game_id: str, install_path: str, platform: str, exe_finder: Callable[[str], str | None]) -> dict[str, Any]:
        """Three-check post-install verification: size, goggame info, exe.

        After ``gogdl install`` returns success,
        we still validate the install is
        usable. Three checks:

        1. **Size ratio** — actual / expected
           must be ≥ 80%. Below this is most
           often an interrupted download where
           gogdl reported success spuriously;
        2. **goggame info** — must exist for
           the launcher to recognise the
           install;
        3. **Executable** — must be locatable
           via the provided ``exe_finder``
           callable.

        Returns a dict — ``complete=True`` is
        the happy path; otherwise ``complete=False``
        plus an ``issue`` string explaining what
        failed (used by the UI to surface).

        Args:
            game_id: product id.
            install_path: install root.
            platform: ``"linux"`` or ``"windows"``.
            exe_finder: callable that locates the
                game exe.

        Returns:
            Dict with verification result.
        """
        try:
            expected = await self.get_expected_disk_size(
                game_id,
                platform,
            )
            actual = GOGFolderOps.folder_size(install_path)
            files = GOGFolderOps.count_files(install_path)
            has_info = GOGFolderOps.has_goggame_info(
                install_path,
            )
            has_exe = exe_finder(install_path) is not None
            size_ratio = (actual / expected) if expected > 0 else 1.0
            logger.info(
                "[GOGInstallPlanner] verify: size=%.1fMB "
                "(%.0f%% of expected), files=%d, "
                "has_info=%s, has_exe=%s",
                actual / (1024 * 1024),
                size_ratio * 100,
                files,
                has_info,
                has_exe,
            )
            if expected > 0 and size_ratio < _MIN_SIZE_RATIO:
                return {
                    "complete": False,
                    "issue": (
                        f"Installation may be incomplete: "
                        f"only {size_ratio * 100:.0f}% of "
                        f"expected size"
                    ),
                    "actual_size": actual,
                    "expected_size": expected,
                    "has_info": has_info,
                    "has_exe": has_exe,
                }
            if not has_info:
                return {
                    "complete": False,
                    "issue": "Missing goggame.info file",
                    "actual_size": actual,
                    "actual_files": files,
                    "has_exe": has_exe,
                }
            if not has_exe:
                return {
                    "complete": False,
                    "issue": "Could not find game executable",
                    "actual_size": actual,
                    "actual_files": files,
                    "has_info": has_info,
                }
            return {
                "complete": True,
                "actual_size": actual,
                "expected_size": expected,
                "actual_files": files,
                "size_ratio": size_ratio,
                "has_info": has_info,
                "has_exe": has_exe,
            }
        except Exception as e:
            logger.error(
                "[GOGInstallPlanner] verify error: %s",
                e,
            )
            return {
                "complete": False,
                "issue": f"Verification failed: {e}",
            }

    async def get_expected_disk_size(self, game_id: str, platform: str) -> int:
        """Run ``gogdl info`` and extract the expected disk_size from the response.

        Used by ``verify_installation`` to compare
        actual vs expected. Returns 0 if gogdl
        can't be invoked or the response doesn't
        contain a parseable size — caller treats
        this as "skip size check".

        Args:
            game_id: product id.
            platform: target.

        Returns:
            Expected size in bytes, or 0.
        """
        gogdl_bin = self._resolve_gogdl_bin()
        if not gogdl_bin:
            return 0
        stdout = await self._spawn_gogdl_info(
            gogdl_bin,
            game_id,
            platform,
        )
        if stdout is None:
            return 0
        return self._parse_size_from_gogdl_info(stdout)

    async def _spawn_gogdl_info(self, gogdl_bin: str, game_id: str, platform: str) -> bytes | None:
        """Spawn ``gogdl info`` with a 30s timeout, return raw stdout.

        Used for size lookups (parsed by
        ``_parse_size_from_gogdl_info``). Errors
        (timeout, OSError) → ``None``.

        Always runs the gogdl-creds cleanup in
        finally to avoid leaking tempdirs.

        Args:
            gogdl_bin: gogdl binary path.
            game_id: product id.
            platform: target.

        Returns:
            Raw stdout, or ``None``.
        """
        cmd = [gogdl_bin, "--auth-config-path", self._config.auth_config_path, "info", "--platform", platform, game_id]
        try:
            env, _gogdl_cleanup = await self._tokens.acquire_gogdl_creds()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout, _stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=30,
                )
                return stdout
            finally:
                await _gogdl_cleanup()
        except (TimeoutError, OSError) as e:
            logger.warning(
                "[GOGInstallPlanner] gogdl info failed: %s",
                e,
            )
            return None

    @staticmethod
    def _parse_size_from_gogdl_info(stdout: bytes) -> int:
        """Walk JSON lines from gogdl info, extract the first valid disk_size.

        Same JSON-lines parsing pattern as
        ``_InstallHelpers.parse_info_output`` —
        each line is a candidate JSON object, we
        try-parse each and take the first one
        whose ``size`` field yields a non-None
        disk size.

        Returns 0 if no line parses or no size
        is extractable.

        Args:
            stdout: raw gogdl output.

        Returns:
            Disk size or 0.
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
            size_info = data.get("size")
            if not isinstance(size_info, dict):
                continue
            extracted = _extract_disk_size_from_size_info(
                size_info,
            )
            if extracted is not None:
                return extracted
        return 0

    async def _cleanup_corrupt_install(self, game_id: str, target_folder: str) -> None:
        """Remove a corrupted install dir + the gogdl support cache for ``game_id``.

        Called when we detect ``has_info=True``
        but size is suspiciously small (gogdl
        was killed mid-install). The support
        cache is removed too because it may
        reference files that no longer exist on
        disk.

        Args:
            game_id: product id.
            target_folder: install dir to remove.
        """

        def _sync() -> None:
            """Blocking rmtree of install dir + support cache for the game.

            Per-OSError logging at ERROR/WARN
            depending on which dir failed; no
            propagation.
            """
            try:
                shutil.rmtree(target_folder)
                logger.info(
                    "[GOGInstallPlanner] removed %s",
                    target_folder,
                )
            except OSError as e:
                logger.error(
                    "[GOGInstallPlanner] corrupt cleanup failed for %s: %s",
                    target_folder,
                    e,
                )
            support_dir = (
                Path(self._config.gogdl_config_dir).expanduser()
                / "gog-support"
                / game_id
            )
            if support_dir.is_dir():
                try:
                    shutil.rmtree(support_dir)
                    logger.info(
                        "[GOGInstallPlanner] cleared support cache: %s",
                        support_dir,
                    )
                except OSError as e:
                    logger.warning(
                        "[GOGInstallPlanner] could not clear support dir: %s",
                        e,
                    )

        await asyncio.to_thread(_sync)

    async def _cleanup_orphaned_install(self, game_id: str, target_folder: str) -> None:
        """Remove orphaned data + stale gogdl manifests.

        Called when we detect data without
        goggame info — usually a leftover from
        a wrong uninstall (user deleted the
        info file manually) or a half-aborted
        install where the info wasn't yet
        written. Either way the data is unusable
        and must go.

        Manifests in known gogdl locations are
        also cleaned because they may reference
        the dead install.

        Args:
            game_id: product id.
            target_folder: dir to remove.
        """

        def _sync() -> None:
            """Blocking rmtree of orphan dir + iterate-unlink the manifest list.

            Per-OSError logging at ERROR/WARN; no
            propagation. Used when data exists
            but goggame info is missing.
            """
            try:
                shutil.rmtree(target_folder)
                logger.info(
                    "[GOGInstallPlanner] removed orphan %s",
                    target_folder,
                )
            except OSError as e:
                logger.error(
                    "[GOGInstallPlanner] orphan cleanup failed: %s",
                    e,
                )
            for manifest_path in self._manifest_locations(
                game_id,
            ):
                mp = Path(manifest_path)
                if mp.is_file():
                    try:
                        mp.unlink()
                        logger.info(
                            "[GOGInstallPlanner] cleaned stale manifest: %s",
                            manifest_path,
                        )
                    except OSError as e:
                        logger.warning(
                            "[GOGInstallPlanner] could not clean manifest: %s",
                            e,
                        )

        await asyncio.to_thread(_sync)

    def _manifest_locations(self, game_id: str) -> list[str]:
        """Compute the known locations gogdl/heroic might cache a manifest.

        gogdl + Heroic Game Launcher have evolved
        through several manifest-location
        schemes. We probe all four to ensure
        clean orphan removal:

        * ``<config>/heroic_gogdl/manifests/<id>``
        * ``<config>../heroic_gogdl/manifests/<id>``
        * ``<config>/manifests/<id>``
        * ``<config>../gogdl/manifests/<id>``

        Args:
            game_id: product id.

        Returns:
            List of candidate paths (some may
            not exist).
        """
        base = Path(self._config.gogdl_config_dir).expanduser()
        parent = base.parent
        return [
            str(base / "heroic_gogdl" / "manifests" / game_id),
            str(parent / "heroic_gogdl" / "manifests" / game_id),
            str(base / "manifests" / game_id),
            str(parent / "gogdl" / "manifests" / game_id),
        ]

    def _resolve_gogdl_bin(self) -> str | None:
        """Read the gogdl bin override (set by ``set_gogdl_bin``).

        Returns ``None`` if not set — caller
        treats that as "gogdl not available,
        skip size checks".

        Returns:
            Path string or ``None``.
        """
        return getattr(self, "_gogdl_bin_override", None)

    def set_gogdl_bin(self, path: str) -> None:
        """Inject the gogdl binary path post-construction.

        The planner is built before the installer
        knows the gogdl path, so we use a setter
        rather than constructor parameter.

        Args:
            path: absolute path to gogdl binary.
        """
        self._gogdl_bin_override = path
