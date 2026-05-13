"""Plugin boot orchestrator — wires the 5-layer architecture.

OP-23b | py_modules/unifideck/bootstrap/boot.py

``boot_plugin`` is the entry point called by ``main.py``
at plugin load. It walks the architecture's five layers
in dependency order:

* **Layer 2 — Core** (``_boot_layer2_core``)
  Event bus + pipeline + cache manager + default cache
  registrations.

* **Layer 3 — Config** (``_boot_config_and_validate``)
  ``ConfigManager`` with defaults + user override paths,
  plus the startup config-validation pass.

* **Layer 4 — Stores** (``_boot_layer4_stores``)
  ``StoreRegistry`` + ``SyncService``, with auto-
  discovery of bundled stores under ``stores/``.

* **Layer 5 — Services** (``_boot_layer5_services``)
  ``ServiceContainer`` with every Layer-5 service
  (shortcut, artwork, cloudsave, …), plus
  ``start_async_services`` to kick off the long-running
  ones.

Each layer attaches its objects directly onto the
``plugin`` instance so RPC handlers can find them by
attribute name.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from unifideck.bootstrap.cache_registry import register_default_caches
from unifideck.bootstrap.pipeline_factory import build_eventbus_pipeline
from unifideck.config import ConfigManager
from unifideck.config.startup import validate_config_at_startup
from unifideck.core.cache_manager import CacheManager
from unifideck.core.sync_service import SyncService
from unifideck.event_bus.event_bus import EventBus
from unifideck.services.bootstrap import (
    bootstrap_services,
    start_async_services,
)
from unifideck.stores import StoreRegistry

logger = logging.getLogger(__name__)


async def boot_plugin(
    plugin: Any,
    *,
    decky_plugin_dir: str,
    user_config_path_resolver: Any,
) -> None:
    """Run the four boot layers + log readiness.

    Keyword-only callable args (``user_config_path_resolver``)
    keep the contract explicit — it's a callable, not a
    path, because the user-config location depends on
    Decky runtime state that isn't available until after
    boot starts.

    Args:
        plugin: the Decky plugin instance to populate
            with attributes.
        decky_plugin_dir: absolute plugin directory.
        user_config_path_resolver: callable returning the
            user-config file path.
    """
    pipeline = await _boot_layer2_core(plugin, decky_plugin_dir)
    await _boot_config_and_validate(
        plugin,
        decky_plugin_dir,
        user_config_path_resolver,
    )
    _boot_layer4_stores(plugin, decky_plugin_dir)
    await _boot_layer5_services(plugin, pipeline)
    logger.info("[Unifideck] plugin loaded")


async def _boot_layer2_core(plugin: Any, decky_plugin_dir: str) -> Any:
    """Wire the bus, the bus pipeline, the cache, and register caches.

    Order matters: the bus must exist before the
    pipeline (which subscribes to it); the cache must
    exist before ``register_default_caches`` is called.

    Args:
        plugin: instance receiving the attributes.
        decky_plugin_dir: plugin root path.

    Returns:
        The built pipeline (passed through to layer 5).
    """
    plugin.bus = EventBus()
    pipeline = await build_eventbus_pipeline(plugin)
    plugin.cache = CacheManager(
        os.path.join(decky_plugin_dir, "data", "cache"),
    )
    register_default_caches(plugin.cache)
    return pipeline


async def _boot_config_and_validate(
    plugin: Any,
    decky_plugin_dir: str,
    user_config_path_resolver: Any,
) -> None:
    """Build ``ConfigManager`` and run the startup validation pass.

    The validation pass produces two outputs stamped on
    the plugin:

    * ``_config_validation_result`` — typed result with
      per-error details, surfaced to the frontend via
      ``UIHandlers.get_config_validation_status``;
    * ``_config_degraded`` — bool quick-check used by
      services to decide whether to opt into safer
      defaults.

    Args:
        plugin: instance receiving the attributes.
        decky_plugin_dir: plugin root.
        user_config_path_resolver: callable returning
            user-config path.
    """
    plugin._user_config_path = user_config_path_resolver()
    defaults_path = os.path.join(
        decky_plugin_dir,
        "defaults",
        "config.json",
    )
    plugin.config = ConfigManager(
        defaults_path=defaults_path,
        user_path=plugin._user_config_path,
    )
    (
        plugin._config_validation_result,
        plugin._config_degraded,
    ) = await validate_config_at_startup(
        bus=plugin.bus,
        config=plugin.config,
        defaults_path=defaults_path,
        user_config_path=plugin._user_config_path,
    )


def _boot_layer4_stores(plugin: Any, decky_plugin_dir: str) -> None:
    """Build store registry + sync service and auto-discover stores.

    Auto-discovery walks ``stores_dir`` and registers
    every store class it finds — see
    ``StoreRegistry.auto_discover`` for the discovery
    rules.

    Args:
        plugin: instance receiving the attributes.
        decky_plugin_dir: plugin root (used to derive
            the stores directory).
    """
    plugin.registry = StoreRegistry(plugin.bus)
    plugin.sync_service = SyncService(plugin.bus, plugin.registry)
    stores_dir = os.path.join(
        decky_plugin_dir,
        "py_modules",
        "unifideck",
        "stores",
    )
    plugin.registry.auto_discover(
        stores_dir,
        plugin_dir=decky_plugin_dir,
        config=plugin.config,
    )


async def _boot_layer5_services(plugin: Any, pipeline: Any) -> None:
    """Construct the service container and start the async services.

    ``bootstrap_services`` returns the typed
    ``ServiceContainer`` with every Layer-5 service
    wired (shortcut, artwork, cloudsave, …).
    ``start_async_services`` then kicks off the
    long-running ones (subscribers, background tasks).

    Args:
        plugin: instance receiving the ``services``
            attribute.
        pipeline: the bus pipeline from
            ``_boot_layer2_core``.
    """
    plugin.services = bootstrap_services(
        plugin.bus,
        plugin.registry,
        plugin.cache,
        plugin.config,
        pipeline,
    )
    await start_async_services(plugin.services)
