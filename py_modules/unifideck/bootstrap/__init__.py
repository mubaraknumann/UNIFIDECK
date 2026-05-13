"""Plugin bootstrap orchestration.

OP-22 | py_modules/unifideck/bootstrap/__init__.py

The four bootstrap modules execute the plugin's startup
sequence:

* ``boot``             — top-level orchestrator: build bus,
  load config, validate, instantiate services, register
  stores.
* ``cache_registry``   — declarative cache registration
  (one call per known cache namespace with its TTL).
* ``pipeline_factory`` — assemble the bus pipeline (plain
  bus → priority dispatcher → watchdog → replay buffer →
  latency collector).
* ``teardown``         — graceful shutdown sequence in
  reverse order.

The orchestrator is intentionally a free function (not a
class) so bootstrap can be re-entered cleanly from test
fixtures without instance-state pollution.
"""
