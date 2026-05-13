"""Central registry of store implementations + auto-discovery + auth dispatch.

OP-25-shared-store-registry
File: py_modules/unifideck/stores/shared/store_registry.py

The ``StoreRegistry`` is the single point that
knows which store implementations are installed and
how to talk to them. Three responsibilities:

* **Registration** — explicit (``register``) and
  auto-discovery (``auto_discover`` scans a
  directory for ``*_store.py`` files, imports
  them, finds the StoreBase subclass, instantiates
  it). Auto-discovery enforces a path-confinement
  security check: the stores dir must live under
  the plugin dir to prevent the user from
  accidentally loading code from arbitrary
  filesystem locations.
* **Lookup** — ``get``, ``get_store``, ``all``,
  ``available``, ``store_ids``, ``has``. Two
  ``get`` variants because some callers want a
  KeyError on miss (strict) and some want None
  (soft).
* **High-level operations** — ``auth_action`` is
  the dispatch entry point used by the RPC layer
  to call ``start_auth`` / ``complete_auth`` /
  ``logout`` / ``status`` on any store by name.
  ``check_all_status`` runs ``is_available`` on
  every store. ``logout_all`` cascades a logout
  across the whole set.

All store-side errors are caught and translated to
``Result`` objects (rather than propagating) so the
RPC layer always returns a well-shaped response.
"""

import asyncio
import logging
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...core.types import Events, Result, StoreError

if TYPE_CHECKING:
    from ...core.cache_manager import CacheManager
    from ...event_bus import EventBus
    from .store_base import StoreBase

logger = logging.getLogger(__name__)


class StoreRegistry:
    """Holds the mapping ``store_id → StoreBase`` and orchestrates store ops.

    Constructor takes the event bus only — stores
    are added later via ``register`` or
    ``auto_discover``. The bus is needed inside
    ``register`` to fire a ``STORE_REGISTERED``
    event when a new store comes online (if an
    event loop is available).
    """

    def __init__(self, bus: "EventBus") -> None:
        """Initialise an empty registry with the given event bus.

        Args:
            bus: ``EventBus`` instance used for
                ``STORE_*`` events.
        """
        self._stores: dict[str, StoreBase] = {}
        self._bus = bus

    def register(self, store_id: str, store: "StoreBase") -> None:
        """Add ``store`` under ``store_id``, firing ``STORE_REGISTERED`` if loop is up.

        Logs at INFO. Tries to schedule an
        ``EventBus.emit`` for ``STORE_REGISTERED``
        but tolerates a missing event loop
        gracefully — registration can happen at
        import time before the loop spins up
        (e.g. during pytest fixtures), and in that
        case the event is suppressed with a DEBUG
        log rather than raising.

        Args:
            store_id: lowercase store name
                (``"epic"``, ``"gog"``, …).
            store: instance to register.
        """
        self._stores[store_id] = store
        logger.info("[StoreRegistry] Registered: %s", store_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "[StoreRegistry] no running event loop; "
                "STORE_REGISTERED suppressed for %s",
                store_id,
            )
            return
        payload = {
            "store_id": store_id,
            "store_info": asdict(store.store_info),
        }
        loop.create_task(
            self._bus.emit(Events.STORE_REGISTERED, **payload),
            name=f"emit_store_registered_{store_id}",
        )

    def auto_discover(self, stores_dir: str, bus: "EventBus", cache: "CacheManager", plugin_dir: str = "", config=None) -> int:
        """Scan ``stores_dir`` for ``*_store.py`` modules, instantiate + register each.

        Five-step pipeline:

        1. ``_validate_stores_dir`` — resolve +
           confine path under plugin_dir. Returns
           None on failure (logged at ERROR with
           SECURITY tag).
        2. Import the ``unifideck.stores``
           parent package so the module names
           resolve.
        3. ``_iter_store_files`` — list candidate
           filenames (skipping ``_*``, symlinks).
        4. ``_load_store_class`` per file — import
           and find the StoreBase subclass.
        5. Instantiate, register, increment
           counter.

        Per-instance failures are caught + logged
        at ERROR without aborting the scan, so one
        broken store doesn't take down the others.

        Args:
            stores_dir: directory to scan.
            bus: event bus (forwarded to
                instances).
            cache: cache manager (forwarded).
            plugin_dir: plugin install root (used
                for path confinement).
            config: optional ConfigManager.

        Returns:
            Number of stores successfully
            registered.
        """
        import importlib

        from .store_base import StoreBase as _StoreBase

        real_stores = self._validate_stores_dir(stores_dir, plugin_dir)
        if real_stores is None:
            return 0
        package_name = "unifideck.stores"
        try:
            importlib.import_module(package_name)
        except ImportError as e:
            logger.warning(
                "[StoreRegistry] Cannot resolve stores package: %s",
                e,
            )
            return 0
        registered = 0
        for filename, full_path in self._iter_store_files(
            real_stores,
        ):
            store_cls = self._load_store_class(
                package_name,
                filename,
                full_path,
                _StoreBase,
            )
            if store_cls is None:
                continue
            try:
                store = store_cls(
                    bus,
                    cache,
                    plugin_dir,
                    config=config,
                )
                store_id = store.store_info.name
                self.register(store_id, store)
                logger.info(
                    "[StoreRegistry] registered %s (%s) from %s",
                    store_id,
                    store_cls.__name__,
                    filename,
                )
                registered += 1
            except Exception as e:
                logger.error(
                    "[StoreRegistry] Failed to instantiate %s from %s: %s",
                    store_cls.__name__,
                    filename,
                    e,
                )
        logger.info(
            "[StoreRegistry] Auto-discovery: %d stores from %s",
            registered,
            real_stores,
        )
        return registered

    @staticmethod
    def _validate_stores_dir(stores_dir: str, plugin_dir: str) -> str | None:
        """Resolve + path-confine the stores dir; return None on any rejection.

        Two security gates:

        * The resolved path must be a directory;
        * If ``plugin_dir`` is non-empty, the
          resolved stores dir must equal or be a
          descendant of ``plugin_dir`` (after
          resolution). Otherwise refuse —
          arbitrary-path loading is a remote-code-
          execution vector.

        When ``plugin_dir`` is empty, log at WARN
        but proceed: unit tests legitimately call
        without it; production must always supply
        it.

        Args:
            stores_dir: candidate directory.
            plugin_dir: plugin install root for
                confinement, or "" to skip.

        Returns:
            Resolved stores dir as string, or
            ``None`` on rejection.
        """
        try:
            real_stores = str(Path(stores_dir).resolve())
        except OSError as e:
            logger.error(
                "[StoreRegistry] Cannot resolve stores dir %r: %s",
                stores_dir,
                e,
            )
            return None
        if not Path(real_stores).is_dir():
            logger.warning(
                "[StoreRegistry] stores dir not found: %s",
                real_stores,
            )
            return None
        if plugin_dir:
            real_plugin = str(Path(plugin_dir).resolve())
            confined = real_stores == real_plugin or real_stores.startswith(
                real_plugin + "/"
            )
            if not confined:
                logger.error(
                    "[StoreRegistry] SECURITY: stores dir "
                    "%s is NOT under plugin dir %s — "
                    "refusing to auto-discover.",
                    real_stores,
                    real_plugin,
                )
                return None
        else:
            logger.warning(
                "[StoreRegistry] auto_discover called "
                "without plugin_dir — path confinement "
                "disabled. This is only acceptable in unit "
                "tests; production must always pass "
                "plugin_dir.",
            )
        return real_stores

    @staticmethod
    def _iter_store_files(real_stores: str):
        """Yield ``(filename, full_path)`` for each ``*_store.py`` in the dir.

        Skips:

        * Files not matching ``*_store.py``;
        * Files starting with ``_`` (convention
          for private modules);
        * Symlinks — logged at WARN with the
          SECURITY tag (a symlink could redirect
          to arbitrary code outside the plugin
          dir, defeating ``_validate_stores_dir``'s
          confinement).

        Iterates the directory in sorted order so
        registration order is deterministic.

        Args:
            real_stores: resolved, confined dir.

        Yields:
            ``(filename, str_path)`` pairs.
        """
        real_stores_p = Path(real_stores)
        for entry in sorted(real_stores_p.iterdir()):
            filename = entry.name
            if not filename.endswith("_store.py"):
                continue
            if filename.startswith("_"):
                continue
            if entry.is_symlink():
                logger.warning(
                    "[StoreRegistry] SECURITY: skipping symlink %s",
                    str(entry),
                )
                continue
            yield filename, str(entry)

    @staticmethod
    def _load_store_class(package_name: str, filename: str, full_path: str, store_base_cls: type) -> type | None:
        """Import the module + return its StoreBase subclass (or None on miss).

        ImportError / runtime error during import
        → log at DEBUG (some modules legitimately
        fail import — missing optional deps —
        and we don't want noisy ERRORs).

        Walks the module's attribute names looking
        for a class that:

        * Is a class;
        * Subclasses ``store_base_cls``;
        * Isn't ``store_base_cls`` itself;
        * Has a ``store_info`` attribute (the
          contract for concrete stores).

        Returns the first match — modules with
        multiple StoreBase subclasses are
        unsupported by design.

        Args:
            package_name: ``"unifideck.stores"``.
            filename: source filename (used to
                derive the submodule name).
            full_path: absolute path (logged).
            store_base_cls: the StoreBase ABC for
                subclass check.

        Returns:
            The concrete class, or ``None``.
        """
        import importlib

        module_name = f"{package_name}.{filename[:-3]}"
        logger.info(
            "[StoreRegistry] loading %s from %s",
            module_name,
            full_path,
        )
        try:
            mod = importlib.import_module(module_name)
        except Exception as e:
            logger.debug(
                "[StoreRegistry] Skip %s: %s",
                filename,
                e,
            )
            return None
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, store_base_cls)
                and attr is not store_base_cls
                and hasattr(attr, "store_info")
            ):
                return attr
        return None

    def get(self, store_id: str) -> "StoreBase":
        """Strict lookup — raises if not registered.

        Args:
            store_id: store name.

        Returns:
            ``StoreBase`` instance.

        Raises:
            KeyError: store isn't registered
                (message includes the list of
                registered ones so the error is
                actionable).
        """
        if store_id not in self._stores:
            raise KeyError(
                f"Store '{store_id}' not registered. "
                f"Available: {list(self._stores.keys())}",
            )
        return self._stores[store_id]

    def get_store(self, store_id: str) -> "StoreBase | None":
        """Soft lookup — returns ``None`` on miss.

        Args:
            store_id: store name.

        Returns:
            Instance or ``None``.
        """
        return self._stores.get(store_id)

    def all(self) -> list["StoreBase"]:
        """Snapshot of every registered store (in registration order).

        Returns:
            List copy of the values.
        """
        return list(self._stores.values())

    def available(self) -> list["StoreBase"]:
        """Stores whose last availability check returned True.

        Uses the cached ``_cached_available`` flag
        set by ``check_all_status`` /
        ``auth_action("status")``; doesn't trigger
        a fresh probe.

        Returns:
            Filtered subset.
        """
        return [s for s in self._stores.values() if getattr(s, "_cached_available", False)]

    def store_ids(self) -> list[str]:
        """Registered store ids in registration order.

        Returns:
            Key list.
        """
        return list(self._stores.keys())

    def has(self, store_id: str) -> bool:
        """Predicate: is ``store_id`` registered?

        Args:
            store_id: store name.

        Returns:
            Membership flag.
        """
        return store_id in self._stores

    def get_store_infos(self) -> list[dict]:
        """Return ``StoreInfo`` dicts for every store, augmented with availability.

        Each dict has all the ``StoreInfo`` fields
        plus an ``available`` boolean (from
        ``_cached_available``). Used by the RPC
        layer to populate the UI's store list.

        Returns:
            List of dicts (one per store).
        """
        infos = []
        for store in self._stores.values():
            info = asdict(store.store_info)
            info["available"] = getattr(
                store,
                "_cached_available",
                False,
            )
            infos.append(info)
        return infos

    async def auth_action(self, store_id: str, action: str, **kwargs) -> Result:
        """Dispatch ``start`` / ``complete`` / ``logout`` / ``status`` to ``store_id``.

        Unknown store → ``Result(success=False)``
        with message.

        Per-action:

        * ``"start"`` / ``"complete"`` → delegate
          to the store's auth method;
        * ``"logout"`` → call ``logout()``; on
          success, emit ``STORE_LOGOUT``;
        * ``"status"`` → call ``is_available()``
          and update the cached flag.

        ``StoreError`` is caught and translated to
        a failed ``Result``, with a
        ``STORE_AUTH_FAILED`` event emitted as a
        side effect. Generic ``Exception`` is
        caught and logged with full traceback +
        translated.

        Args:
            store_id: store name.
            action: ``"start"`` / ``"complete"`` /
                ``"logout"`` / ``"status"``.
            **kwargs: passed to the store method.

        Returns:
            ``Result``.
        """
        try:
            store = self.get(store_id)
        except KeyError as e:
            return Result(success=False, error=str(e))
        try:
            if action == "start":
                return await store.start_auth(**kwargs)
            if action == "complete":
                return await store.complete_auth(**kwargs)
            if action == "logout":
                result = await store.logout()
                if result.success:
                    await self._bus.emit(
                        Events.STORE_LOGOUT,
                        store=store_id,
                    )
                return result
            if action == "status":
                is_avail = await store.is_available()
                store._cached_available = is_avail
                return Result(success=is_avail)
            return Result(
                success=False,
                error=(
                    f"Unknown auth action: '{action}'. "
                    f"Valid: start, complete, logout, status"
                ),
            )
        except StoreError as e:
            logger.error(
                "[StoreRegistry] %s.%s failed: %s",
                store_id,
                action,
                e,
            )
            await self._bus.emit(
                Events.STORE_AUTH_FAILED,
                store=store_id,
                error=str(e),
            )
            return Result(success=False, error=str(e))
        except Exception as e:
            logger.exception(
                "[StoreRegistry] Unexpected error in %s.%s",
                store_id,
                action,
            )
            return Result(
                success=False,
                error=f"Unexpected: {e}",
            )

    async def check_all_status(self) -> list[dict[str, Any]]:
        """Probe ``is_available`` on every store and return a status list.

        Per-store: call ``is_available()``,
        capture the result, update the cached
        flag. On exception, store the message in
        ``entry["error"]`` and log at WARN —
        treats it the same as "unavailable".

        Each entry has:
        ``{"store_id", "name", "available",
        "error"}``.

        Returns:
            List of status dicts (one per
            registered store).
        """
        results: list[dict[str, Any]] = []
        for store in self._stores.values():
            entry: dict[str, Any] = {
                "store_id": store.store_info.name,
                "name": store.store_info.display_name,
                "available": False,
                "error": None,
            }
            try:
                entry["available"] = await store.is_available()
                store._cached_available = entry["available"]
            except Exception as e:
                entry["error"] = str(e)
                logger.warning(
                    "[StoreRegistry] %s availability check failed: %s",
                    store.store_info.name,
                    e,
                )
            results.append(entry)
        return results

    async def logout_all(self) -> dict[str, Any]:
        """Sequentially logout every registered store, returning per-store outcomes.

        Each store's ``Result`` is normalised to a
        ``{success, error}`` dict. Used by the
        "factory reset" flow.

        Returns:
            Mapping ``store_id → {success, error}``.
        """
        out: dict[str, Any] = {}
        for store_id in self._stores:
            result = await self.auth_action(store_id, "logout")
            out[store_id] = {
                "success": result.success,
                "error": result.error,
            }
        return out
