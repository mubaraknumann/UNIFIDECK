"""
Wine prefix lifecycle manager for Ubisoft games.

OP-59a | py_modules/unifideck/stores/ubisoft/prefix/manager.py

``UbisoftPrefixManager`` owns the creation, validation, and destruction
of Wine prefixes used by Ubisoft games. Three categories of prefix
coexist:

1. **template prefix** (``.template``) — UPC-installed-but-no-game;
   used as the base for fresh installs (avoid running the UPC installer
   for every game).
2. **auth prefix** (``.upc-auth``) — used solely by the auth flow.
3. **per-game prefixes** — one per installed game.

The manager exposes ``ensure_template``, ``ensure_auth``, ``create_for_game``,
``destroy``, and ``validate``. Each operation is delegated to one of
``template_builder.py`` / ``auth_builder.py`` / ``helpers.py``.
"""

from __future__ import annotations
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from ..binaries import UbisoftBinaryResolver
from ..config import UbisoftConfig
from ..installer.cache import UbisoftInstallerCache
from ..paths import UbisoftPrefixPaths
from .auth_builder import _AuthPrefixBuilder
from .helpers import _PrefixHelpers
from .template_builder import _TemplatePrefixBuilder

logger = logging.getLogger(__name__)


class UbisoftPrefixManager:
    """Lifecycle manager for the three categories of Wine prefix.

    Coordinates ``_TemplatePrefixBuilder``, ``_AuthPrefixBuilder``,
    and ``_PrefixHelpers`` to expose a unified API: ensure-template,
    ensure-auth, bootstrap-game-prefix, repair-prefix.
    """

    def __init__(
        self,
        config: UbisoftConfig,
        paths: UbisoftPrefixPaths,
        binaries: UbisoftBinaryResolver,
        installer_cache: UbisoftInstallerCache,
        inject_auth_state: Callable[[list[str]], int],
    ) -> None:
        """Wire dependencies and build the prefix-helper sub-orchestrators.

        Builds the prefix helpers, the template-prefix builder
        (used to seed new game prefixes from a known-good template),
        and the auth-prefix builder (specialised for the auth
        shortcut's prefix).

        Args:
            config: Ubisoft store config.
            paths: Ubisoft prefix paths.
            binaries: Ubisoft binary resolver.
            installer_cache: Cached UPC installer artefacts.
            inject_auth_state: Callable injecting the captured
                Ubisoft auth state into one or more prefixes
                (returns the number of prefixes patched).
        """
        self._config = config
        self._paths = paths
        self._binaries = binaries
        self._installer_cache = installer_cache
        self._inject_auth_state = inject_auth_state
        self._helpers = _PrefixHelpers(self)
        self._template_builder = _TemplatePrefixBuilder(
            config=config,
            paths=paths,
            helpers=self._helpers,
            installer_cache=installer_cache,
        )
        self._auth_builder = _AuthPrefixBuilder(
            config=config,
            paths=paths,
            helpers=self._helpers,
            installer_cache=installer_cache,
            template_builder=self._template_builder,
        )

    def template_exists(self) -> bool:
        """Check whether the Ubisoft template prefix exists on disk.

        Returns:
            True iff the template directory is present.
        """
        return self._template_builder.template_exists()

    def is_prefix_version_stale(self, prefix_dir: str) -> bool:
        """Check whether a per-game prefix was built with an older template version.

        Compares the prefix's stamp against the current template
        version; mismatches mean the prefix should be regenerated.

        Args:
            prefix_path: Per-game prefix path.

        Returns:
            True iff the prefix version is older than the current
            template (or no stamp could be read).
        """
        return self._template_builder.is_prefix_version_stale(
            prefix_dir,
        )

    @staticmethod
    def read_machine_guid(prefix_path: str) -> str:
        """Read the MachineGuid registry value from one prefix.

        Args:
            prefix_path: Prefix to inspect.

        Returns:
            The MachineGuid value, or ``None`` if absent.
        """
        return _TemplatePrefixBuilder.read_machine_guid(prefix_path)

    def queue_template_creation(self) -> None:
        """Queue an async template-prefix creation task.

        Fire-and-forget: returns immediately; the actual work
        runs in the background task pool.
        """
        self._template_builder.queue_template_creation()

    async def regenerate_template_if_stale(self) -> None:
        """Recreate the template prefix if its version is older than current.

        No-op when the template is already up to date.

        Returns:
            True iff the template was regenerated.
        """
        await self._template_builder.regenerate_template_if_stale()

    async def ensure_template_prefix(self) -> None:
        """Make sure the template prefix exists, regenerating it if needed.

        Returns:
            True iff the template is ready after this call.
        """
        await self._template_builder.ensure_template_prefix()

    async def ensure_auth_prefix(self) -> str | None:
        """Make sure the auth prefix exists, materialising it if needed.

        Returns:
            True iff the auth prefix is ready after this call.
        """
        return await self._auth_builder.ensure_auth_prefix()

    def queue_auth_assets_ensure(
        self,
        reason: str = "background",
    ) -> None:
        """Queue an async refresh of the auth prefix's UPC assets.

        Args:
            space_id: Triggering game's space_id (used for logging).
        """
        self._auth_builder.queue_auth_assets_ensure(reason)

    async def bootstrap_game_prefix(self, space_id: str) -> bool:
        """Build the per-game prefix (template clone or fresh install).

        Fast path: if the prefix already has the marker AND upc.exe,
        just re-inject auth state and return success. Slow path: try
        template clone; fall back to fresh install if no template
        exists.

        Args:
            space_id: UPC space_id.

        Returns:
            True iff the prefix is ready to launch the game.
        """
        prefix_path = self._paths.get_prefix_path(space_id)
        marker_path = Path(prefix_path) / self._config.bootstrap_marker
        if marker_path.is_file() and self._paths.find_upc_exe(prefix_path):
            self._helpers.try_inject_auth_state([prefix_path])
            return True
        if (
            self._template_builder.template_exists()
            and await self._helpers.clone_prefix_from_template(
                space_id,
                prefix_path,
            )
        ):
            return True
        return await self._helpers.create_prefix_from_fresh_install(
            space_id,
            prefix_path,
        )

    async def repair_prefix(
        self,
        space_id: str,
    ) -> bool:
        """Destroy and rebuild a per-game prefix from scratch.

        Wipes the existing prefix directory, then re-runs the full
        bootstrap path. Used when a prefix gets into an inconsistent
        state that simple injection can't fix.

        Args:
            space_id: UPC space_id.

        Returns:
            True iff the rebuild succeeded.
        """
        prefix_path = self._paths.get_prefix_path(space_id)
        logger.info(
            "[UbisoftPrefixManager] repairing prefix for %s",
            space_id,
        )
        try:
            if Path(prefix_path).is_dir():
                shutil.rmtree(prefix_path)
                logger.info(
                    "[UbisoftPrefixManager] removed corrupted prefix for %s",
                    space_id,
                )
        except OSError as e:
            logger.error(
                "[UbisoftPrefixManager] could not remove corrupted prefix: %s",
                e,
            )
            return False
        return await self.bootstrap_game_prefix(space_id)
