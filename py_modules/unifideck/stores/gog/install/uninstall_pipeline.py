"""GOG uninstall pipeline — multi-attempt rmtree with force-cleanup fallback.

OP-22-gog-install-uninstall
File: py_modules/unifideck/stores/gog/install/uninstall_pipeline.py

GOG games can leave file handles open (Wine/Proton
running game processes, antivirus scanners,
GameMode services), so a single ``rmtree`` may
fail mid-way and leave a partial dir behind.

Strategy:

1. Try ``shutil.rmtree`` up to
   ``_UNINSTALL_MAX_ATTEMPTS=3`` times — each
   attempt may free up more handles;
2. On the third attempt's failure, fall back to
   ``GOGFolderOps.force_cleanup_folder`` which
   does an aggressive bottom-up walk and ignores
   errors;
3. After cleanup, wipe the support cache and
   manifests (parent ``GOGInstaller``);
4. Final check — if files remain, return failure
   with the count in the error code so the UI can
   show what to delete manually.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import TYPE_CHECKING

from ....core.types import Result
from .primitives import GOGFolderOps

if TYPE_CHECKING:
    from .installer import GOGInstaller

logger = logging.getLogger(__name__)
_UNINSTALL_MAX_ATTEMPTS = 3


class _UninstallPipeline:
    """Encapsulates the uninstall flow — internal collaborator of ``GOGInstaller``.

    Holds a back-reference to its parent so it
    can call the parent's private cache/manifest
    wipe methods. This is a deliberate split:
    the public installer should not be hundreds
    of lines long.
    """

    def __init__(self, parent: GOGInstaller) -> None:
        """Stash the parent reference.

        Args:
            parent: ``GOGInstaller`` instance —
                used for support-cache and
                manifest wipes.
        """
        self._parent = parent

    async def uninstall_game(
        self,
        game_id: str,
        install_path: str | None = None,
    ) -> Result:
        """Remove the install dir + support cache + manifests for ``game_id``.

        Pipeline:

        1. Missing path → success (idempotent —
           re-running uninstall on an
           already-removed game is fine);
        2. Loop up to 3 rmtree attempts, logging
           permission vs other OSErrors
           separately;
        3. If the dir is gone, success — break;
        4. Otherwise log remaining file count
           (helps diagnose which subdir is
           sticky);
        5. On the third attempt's failure, run
           ``force_cleanup_folder`` (aggressive
           bottom-up rm);
        6. Wipe support cache + manifests via
           parent;
        7. Final probe — files left over →
           return ``uninstall_incomplete_<N>_remaining``
           so the UI can surface the count.

        Args:
            game_id: GOG product id.
            install_path: directory to remove.
                Optional — missing path returns
                success.

        Returns:
            ``Result`` (success or specific
            error code).
        """
        if not install_path or not os.path.exists(install_path):
            logger.info(
                "[GOGInstaller] %s already gone, nothing to do",
                game_id,
            )
            return Result(success=True)
        for attempt in range(_UNINSTALL_MAX_ATTEMPTS):
            try:
                await asyncio.to_thread(
                    shutil.rmtree,
                    install_path,
                )
            except PermissionError as e:
                logger.warning(
                    "[GOGInstaller] attempt %d permission: %s",
                    attempt + 1,
                    e,
                )
            except OSError as e:
                logger.warning(
                    "[GOGInstaller] attempt %d failed: %s",
                    attempt + 1,
                    e,
                )
            if not os.path.exists(install_path):
                logger.info(
                    "[GOGInstaller] uninstalled %s",
                    install_path,
                )
                break
            remaining = GOGFolderOps.count_files(install_path)
            logger.warning(
                "[GOGInstaller] attempt %d: %d files remain",
                attempt + 1,
                remaining,
            )
            if attempt == _UNINSTALL_MAX_ATTEMPTS - 1:
                logger.info(
                    "[GOGInstaller] falling back to force cleanup",
                )
                await GOGFolderOps.force_cleanup_folder(
                    install_path,
                )
        await self._parent._wipe_support_cache(game_id)
        await self._parent._wipe_manifests(game_id)
        if os.path.exists(install_path):
            remaining = GOGFolderOps.count_files(install_path)
            if remaining > 0:
                return Result(
                    success=False,
                    error=(f"uninstall_incomplete_{remaining}_remaining"),
                )
        return Result(success=True)
