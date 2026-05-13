"""Plugin teardown — symmetric counterpart to ``boot_plugin``.

OP-23d | py_modules/unifideck/bootstrap/teardown.py

Reverses the boot sequence in the same order:

1. Stop every Layer-5 service (cancels long-running
   tasks, flushes pending writes).
2. Stop the ``PriorityDispatcher`` if it was wired (drains
   the per-priority queues).
3. Clear the bus's subscriber tables.

Each step logs at INFO so the unload trace is visible
in plugin logs — useful when diagnosing "plugin won't
reload" issues.
"""

from __future__ import annotations

import logging
from typing import Any

from unifideck.services.bootstrap import stop_all_services

logger = logging.getLogger(__name__)


async def unload_plugin(plugin: Any) -> None:
    """Tear down every layer attached to ``plugin`` in reverse boot order.

    Defensive ``hasattr`` check on the dispatcher
    handles the edge case where boot failed partway
    through (the dispatcher might not have been wired
    yet). ``bus.clear()`` is called unconditionally
    because the bus is the first thing built and the
    last thing to go.

    Args:
        plugin: the plugin instance (must have ``services``
            and ``bus`` attributes from a prior boot).
    """
    await stop_all_services(plugin.services)
    if hasattr(plugin, "dispatcher") and plugin.dispatcher is not None:
        await plugin.dispatcher.stop()
        logger.info("[Unifideck] PriorityDispatcher stopped")
    plugin.bus.clear()
    logger.info("[Unifideck] unload complete")
