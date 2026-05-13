"""Bus pipeline assembly — wires watchdog + latency + replay + dispatcher.

OP-23c | py_modules/unifideck/bootstrap/pipeline_factory.py

Builds the full bus observability + supervision pipeline
in one place. The pipeline has five collaborators:

* ``HandlerWatchdog``        — per-handler timeout
  detection + quarantine;
* ``HandlerLatencyCollector`` — p50/p95/top-N latency
  stats;
* ``EventReplayBuffer``      — bounded ring of recent
  events (diagnostics);
* ``BatchDispatcher``        — handler coalescing for
  high-volume bursts;
* ``PriorityDispatcher``     — main per-event scheduler
  that ties all of the above together.

Each instance is stamped onto the plugin so the
observability RPC and other consumers can reach them by
attribute. The returned ``BusPipeline`` is a typed
container with the same references — convenient for
testing where the pipeline is passed around as one object.

Imports are deferred to function body so the bootstrap
module load is cheap (these submodules import heavy
dependencies of their own).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from unifideck.event_bus.priority_dispatcher import PriorityDispatcher

if TYPE_CHECKING:
    from unifideck.event_bus.bus_pipeline import BusPipeline

logger = logging.getLogger(__name__)


async def build_eventbus_pipeline(plugin: Any) -> BusPipeline:
    """Construct every pipeline collaborator + start the dispatcher.

    Pipeline assembly order:

    1. Construct the four passive collaborators
       (watchdog, latency, replay, batcher) — cheap, no
       running tasks.
    2. Build the ``PriorityDispatcher`` with them all
       injected.
    3. ``await dispatcher.start()`` — this kicks off the
       background per-priority workers.

    All collaborators get attached to the plugin instance
    for downstream attribute-based lookup by the
    observability RPC.

    Args:
        plugin: plugin instance receiving the attributes.

    Returns:
        ``BusPipeline`` typed container.
    """
    from unifideck.event_bus.bus_pipeline import BusPipeline
    from unifideck.event_bus.event_bus_scaling import BatchDispatcher
    from unifideck.event_bus.event_replay import EventReplayBuffer
    from unifideck.event_bus.supervision.metrics_handler import (
        HandlerLatencyCollector,
    )
    from unifideck.event_bus.supervision.watchdog_handler import HandlerWatchdog

    plugin.watchdog = HandlerWatchdog()
    plugin.latency = HandlerLatencyCollector()
    plugin.replay = EventReplayBuffer()
    plugin.batcher = BatchDispatcher()
    plugin.dispatcher = PriorityDispatcher(
        plugin.bus,
        watchdog=plugin.watchdog,
        latency_collector=plugin.latency,
        replay_buffer=plugin.replay,
        batch_dispatcher=plugin.batcher,
    )
    await plugin.dispatcher.start()
    logger.info(
        "[Unifideck] EventBus pipeline ready: dispatcher + "
        "watchdog + metrics + replay + batch",
    )
    return BusPipeline(
        watchdog=plugin.watchdog,
        latency=plugin.latency,
        replay=plugin.replay,
        batcher=plugin.batcher,
        dispatcher=plugin.dispatcher,
    )
