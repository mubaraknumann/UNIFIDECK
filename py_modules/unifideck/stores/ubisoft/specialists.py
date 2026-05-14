"""
Per-game specialists — engine-specific quirks for selected Ubisoft titles.

OP-55j | py_modules/unifideck/stores/ubisoft/specialists.py

A handful of Ubisoft games need special handling because their engine
(e.g. AnvilNext, Dunia, Snowdrop) has known interactions with Wine /
Proton: anti-cheat layers, DRM-bound saves, controller-glyph remapping,
etc. This module groups the specialist classes that adjust the
generic install/launch flow for those titles.

The dispatch is by game ID (or executable name fingerprint), and the
specialist receives the standard launch context and may mutate the
environment variables, working directory, or launch arguments before
the actual ``proton run`` is invoked.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Any
from unifideck.stores.ubisoft.auth import (
    UbisoftAuth,
    UbisoftAuthServices,
    UbisoftAuthState,
)
from unifideck.stores.ubisoft.binaries import UbisoftBinaryResolver
from unifideck.stores.ubisoft.config import UbisoftConfig
from unifideck.stores.ubisoft.id_map import UbisoftIdMap
from unifideck.stores.ubisoft.installer import UbisoftInstaller
from unifideck.stores.ubisoft.installer.cache import (
    UbisoftInstallerCache,
)
from unifideck.stores.ubisoft.library import UbisoftLibrary
from unifideck.stores.ubisoft.paths import UbisoftPrefixPaths
from unifideck.stores.ubisoft.prefix import UbisoftPrefixManager
from unifideck.stores.ubisoft.session import UbisoftSession

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UbisoftSpecialists:
    """Frozen bundle of every per-domain specialist used by the Ubisoft store.

    Built by ``build_ubisoft_specialists`` and stored as a
    single attribute on ``UbisoftStore``; the store unpacks
    the relevant fields onto its own attributes for direct use.

    Attributes:
        config: Frozen Ubisoft configuration.
        paths: Wine prefix path helpers.
        binaries: UPC binary resolver.
        id_map: Space_id ↔ install_id mapping store.
        session: Session payload propagator.
        installer_cache: Cached UPC installer downloader.
        prefix_mgr: Wine prefix lifecycle manager.
        library: Library facade (detector + id map).
        installer: Install/uninstall pipeline.
        auth: Auth flow orchestrator.
    """

    config: UbisoftConfig
    paths: UbisoftPrefixPaths
    binaries: UbisoftBinaryResolver
    id_map: UbisoftIdMap
    session: UbisoftSession
    installer_cache: UbisoftInstallerCache
    prefix_mgr: UbisoftPrefixManager
    library: UbisoftLibrary
    installer: UbisoftInstaller
    auth: UbisoftAuth


@dataclass(frozen=True)
class _UbisoftFoundations:
    """Layer-0 bundle: dependencies that need only the ConfigManager + plugin_dir.

    Built first by the factory so every later layer can depend
    on these without circular references.

    Attributes:
        ubi_config: Frozen Ubisoft configuration snapshot.
        paths: Wine prefix path helpers.
        binaries: UPC binary resolver.
        id_map: Space_id ↔ install_id mapping store.
    """

    ubi_config: UbisoftConfig
    paths: UbisoftPrefixPaths
    binaries: Any
    id_map: UbisoftIdMap


@dataclass(frozen=True)
class _UbisoftRuntimeChain:
    """Layer-1 bundle: runtime services layered on top of the foundations.

    Attributes:
        session: Session payload propagator.
        installer_cache: Cached UPC installer downloader.
        prefix_mgr: Wine prefix lifecycle manager.
    """

    session: UbisoftSession
    installer_cache: UbisoftInstallerCache
    prefix_mgr: UbisoftPrefixManager


def _build_ubisoft_foundations(
    config_mgr: Any,
    plugin_dir: str | None,
) -> _UbisoftFoundations:
    """Construct the foundation layer (config, paths, binaries, id_map).

    These four objects depend only on the ConfigManager and the
    plugin directory; everything else in the Ubisoft sub-package
    is layered on top.

    Args:
        config_mgr: ConfigManager.
        plugin_dir: Plugin root directory (for bundled binaries).

    Returns:
        A ``_UbisoftFoundations`` bundle.
    """
    ubi_config = UbisoftConfig.from_config_manager(config_mgr)
    logger.info("[UbisoftStore] %s", ubi_config.describe())
    paths = UbisoftPrefixPaths(ubi_config)
    binaries = UbisoftBinaryResolver(ubi_config, plugin_dir)
    id_map = UbisoftIdMap(ubi_config, paths)
    return _UbisoftFoundations(
        ubi_config=ubi_config,
        paths=paths,
        binaries=binaries,
        id_map=id_map,
    )


def _build_ubisoft_runtime_chain(
    f: _UbisoftFoundations,
) -> _UbisoftRuntimeChain:
    """Construct the runtime layer (session, installer_cache, prefix_mgr).

    These three depend on the foundations and on each other —
    ``prefix_mgr`` consumes ``session`` for auth-state injection.

    Args:
        f: The previously-built foundations bundle.

    Returns:
        A ``_UbisoftRuntimeChain`` bundle.
    """
    session = UbisoftSession(
        config=f.ubi_config,
        paths=f.paths,
        read_machine_guid=UbisoftPrefixManager.read_machine_guid,
    )
    installer_cache = UbisoftInstallerCache(f.ubi_config)
    prefix_mgr = UbisoftPrefixManager(
        config=f.ubi_config,
        paths=f.paths,
        binaries=f.binaries,
        installer_cache=installer_cache,
        inject_auth_state=session.ensure_auth_state_in_prefixes,
    )
    return _UbisoftRuntimeChain(
        session=session,
        installer_cache=installer_cache,
        prefix_mgr=prefix_mgr,
    )


def _build_ubisoft_auth(
    *,
    bus: Any,
    f: _UbisoftFoundations,
    r: _UbisoftRuntimeChain,
    plugin_dir: str | None,
    shortcut_service: Any | None,
    steamgriddb: Any | None,
) -> UbisoftAuth:
    """Build the ``UbisoftAuth`` orchestrator from its dependencies.

    Args:
        bus: Event bus.
        foundations: Layer-0 dependencies.
        runtime: Layer-1 dependencies.
        plugin_dir: Plugin root directory.
        shortcut_service: Steam shortcut service (optional).
        steamgriddb: SteamGridDB client (optional).

    Returns:
        A ready-to-use ``UbisoftAuth``.
    """
    return UbisoftAuth(
        bus=bus,
        state=UbisoftAuthState(
            config=f.ubi_config,
            paths=f.paths,
            binaries=f.binaries,
            session=r.session,
            ensure_auth_prefix=r.prefix_mgr.ensure_auth_prefix,
            queue_auth_assets_ensure=r.prefix_mgr.queue_auth_assets_ensure,
        ),
        services=UbisoftAuthServices(
            plugin_dir=plugin_dir,
            shortcut_service=shortcut_service,
            steamgriddb=steamgriddb,
        ),
    )


def build_ubisoft_specialists(
    *,
    bus: Any,
    config_mgr: Any,
    plugin_dir: str | None,
    shortcut_service: Any | None,
    steamgriddb: Any | None,
) -> UbisoftSpecialists:
    """Build the full sub-package object graph for ``UbisoftStore``.

    Layered construction: foundations → runtime chain → auth →
    library / installer (which depend on auth). Returns a single
    frozen container the store unwraps into its instance attributes.

    Args:
        Various dependencies (bus, config_mgr, plugin_dir,
        shortcut_service, steamgriddb) supplied by the caller.

    Returns:
        A populated ``UbisoftSpecialists``.
    """
    f = _build_ubisoft_foundations(config_mgr, plugin_dir)
    r = _build_ubisoft_runtime_chain(f)
    library = UbisoftLibrary(
        config=f.ubi_config,
        paths=f.paths,
        id_map=f.id_map,
        queue_template_creation=r.prefix_mgr.queue_template_creation,
    )
    installer = UbisoftInstaller(
        config=f.ubi_config,
        paths=f.paths,
        binaries=f.binaries,
        id_map=f.id_map,
        session=r.session,
        library=library,
        bootstrap_game_prefix=r.prefix_mgr.bootstrap_game_prefix,
    )
    auth = _build_ubisoft_auth(
        bus=bus,
        f=f,
        r=r,
        plugin_dir=plugin_dir,
        shortcut_service=shortcut_service,
        steamgriddb=steamgriddb,
    )
    logger.info(
        "[UbisoftStore] fully initialized with 8 specialists",
    )
    return UbisoftSpecialists(
        config=f.ubi_config,
        paths=f.paths,
        binaries=f.binaries,
        id_map=f.id_map,
        session=r.session,
        installer_cache=r.installer_cache,
        prefix_mgr=r.prefix_mgr,
        library=library,
        installer=installer,
        auth=auth,
    )
