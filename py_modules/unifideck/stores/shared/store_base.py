"""Abstract base class for all store implementations.

OP-25-shared-store-base
File: py_modules/unifideck/stores/shared/store_base.py

Every concrete store class (EpicStore, GogStore,
AmazonStore, MicrosoftStore, UbisoftStore) inherits
from ``StoreBase`` and implements the abstract
methods. This base provides:

* **Lifecycle scaffolding** — constructor takes
  the event bus, cache, plugin dir, and optional
  config, stored on the instance for subclass use.
* **Identity** — the ``store_info`` class
  attribute (overridden by subclasses) and
  derived ``store_name`` property.
* **Abstract API surface** — the contract that
  defines what every store must implement:
  availability, auth flows (start + complete),
  logout, library fetch, install / uninstall /
  update game, update check, and per-game size.
* **Common helpers** — ``_find_binary`` (resolves
  store CLIs through ``binary_resolver``),
  ``_find_exe`` (locates game executables in an
  install dir), ``_emit`` (event bus shortcut),
  and ``_run_cli`` (subprocess wrapper with
  timeout + error mapping).

The ``_run_cli`` helper is the single point where
subprocess invocations are bounded by timeout and
where non-zero exits get translated to
``StoreError``, so every store gets that behaviour
"for free".
"""

import asyncio
import logging
import os
import subprocess
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Optional

from ...core.bin import binary_resolver
from ...core.exe_finder import exe_finder
from ...core.types import (
    AuthResult,
    CLITool,
    Events,
    Game,
    InstallResult,
    Result,
    StoreError,
    StoreInfo,
)

if TYPE_CHECKING:
    from ...config import ConfigManager
    from ...core.cache_manager import CacheManager
    from ...event_bus import EventBus

logger = logging.getLogger(__name__)


class StoreBase(ABC):
    """Common scaffolding + abstract contract for every store implementation.

    Subclasses must:

    * Override ``store_info`` with a populated
      ``StoreInfo`` (name, display_name,
      auth_method, icon_asset);
    * Implement all the ``@abstractmethod`` async
      methods.

    Subclasses may use the protected helpers
    (``_find_binary``, ``_find_exe``, ``_emit``,
    ``_run_cli``) directly. Attributes prefixed
    ``_`` are not part of the public surface.
    """

    store_info: StoreInfo = StoreInfo(
        name="unknown",
        display_name="Unknown",
        auth_method="manual",
        icon_asset="",
    )

    def __init__(
        self,
        bus: "EventBus",
        cache: "CacheManager",
        plugin_dir: str | None = None,
        config: Optional["ConfigManager"] = None,
    ) -> None:
        """Stash the injected services on the instance.

        Args:
            bus: event bus for emitting
                progress/status events.
            cache: shared cache manager (used by
                subclasses to memoise library
                fetches, install state, etc.).
            plugin_dir: plugin install root,
                stringified. ``None`` only in
                tests.
            config: optional ConfigManager. Some
                stores need it (e.g. for proxy
                settings), some don't.
        """
        self._bus = bus
        self._cache = cache
        self._plugin_dir = plugin_dir
        self._config = config
        self._cached_available: bool = False

    @property
    def store_name(self) -> str:
        """Convenience shortcut for ``self.store_info.name``.

        Returns:
            Lowercase store identifier
            (``"epic"``, ``"gog"``, …).
        """
        return self.store_info.name

    @abstractmethod
    async def is_available(self) -> bool:
        """Return True if the store can be used (binaries present, auth ok).

        Subclasses typically check for CLI binaries
        on disk and any required network endpoints.

        Returns:
            Availability flag.
        """
        ...

    @abstractmethod
    async def start_auth(self, **kwargs) -> AuthResult:
        """Begin the auth flow (return device-code URL, OAuth state, etc.).

        For OAuth-style stores the result contains
        a code/URL the user must visit. For
        username/password stores it may complete
        immediately.

        Args:
            **kwargs: store-specific (e.g.
                ``username``).

        Returns:
            ``AuthResult`` carrying next-step info.
        """
        ...

    @abstractmethod
    async def complete_auth(self, **kwargs) -> AuthResult:
        """Finish the auth flow after the user's external action.

        Called once the user has clicked through the
        device-code page or supplied the second
        factor.

        Args:
            **kwargs: store-specific.

        Returns:
            ``AuthResult`` (success ⇒ tokens
            persisted).
        """
        ...

    @abstractmethod
    async def logout(self) -> Result:
        """Revoke local tokens (and remote if the store supports it).

        Returns:
            ``Result`` carrying success/failure.
        """
        ...

    @abstractmethod
    async def get_library(self) -> list[Game] | None:
        """Fetch the user's owned-games list.

        Returns:
            List of ``Game`` records, or ``None``
            on failure (the caller distinguishes
            ``None`` from empty list).
        """
        ...

    @abstractmethod
    async def install_game(self, game_id: str, **kwargs: Any) -> InstallResult:
        """Install ``game_id`` to disk.

        Args:
            game_id: store-side game id.
            **kwargs: optional ``install_path``,
                ``language``, ``with_dlcs``, etc.

        Returns:
            ``InstallResult`` with progress channel
            + final state.
        """
        ...

    @abstractmethod
    async def uninstall_game(self, game_id: str, **kwargs: Any) -> Result:
        """Remove ``game_id`` from disk (and unregister from the store CLI).

        Args:
            game_id: store-side game id.

        Returns:
            ``Result``.
        """
        ...

    @abstractmethod
    async def update_game(self, game_id: str, **kwargs: Any) -> InstallResult:
        """Apply available updates to ``game_id``.

        Args:
            game_id: store-side game id.

        Returns:
            ``InstallResult`` (same shape as
            install).
        """
        ...

    @abstractmethod
    async def check_for_updates(self) -> list[str]:
        """List game ids for which the store reports an available update.

        Returns:
            Subset of installed game ids.
        """
        ...

    @abstractmethod
    async def get_game_size(self, game_id: str) -> int | None:
        """Return the install size in bytes (downloaded + unpacked).

        Args:
            game_id: store-side game id.

        Returns:
            Size in bytes, or ``None`` if unknown.
        """
        ...

    def _find_binary(self, tool: CLITool) -> str | None:
        """Delegate to the shared ``binary_resolver`` for CLI paths.

        Args:
            tool: ``CLITool`` enum entry
                (LEGENDARY, GOGDL, …).

        Returns:
            Absolute path or ``None``.
        """
        return binary_resolver.resolve(tool)

    def _find_exe(self, install_path: str, hints: list[str] | None = None) -> str | None:
        """Delegate to ``exe_finder`` to locate the main game .exe in ``install_path``.

        Args:
            install_path: absolute install dir.
            hints: optional list of preferred
                filename substrings (handles cases
                where heuristics pick the wrong
                launcher stub).

        Returns:
            Absolute exe path, or ``None``.
        """
        return exe_finder.find(install_path, hints)

    async def _emit(self, event: Events, **kwargs) -> None:
        """Forward an event onto the bus with arbitrary payload.

        Thin wrapper so subclasses don't have to
        import ``EventBus`` directly.

        Args:
            event: ``Events`` enum entry.
            **kwargs: payload.
        """
        await self._bus.emit(event, **kwargs)

    async def _run_cli(self, args: list[str], binary_path: str | None = None, timeout: int = 300, env: dict[str, str] | None = None) -> str:
        """Spawn a CLI subprocess with timeout, return stdout, map errors to StoreError.

        Pipeline:

        1. Resolve the binary path: explicit
           argument, else ``self.cli_path``
           attribute on the subclass;
        2. Build the env: start with
           ``os.environ``, overlay any
           ``env`` overrides;
        3. ``subprocess.run`` inside
           ``asyncio.to_thread`` so the event loop
           isn't blocked;
        4. On non-zero rc: raise ``StoreError``
           (stderr truncated to 500 chars to keep
           logs scannable);
        5. On timeout: raise ``StoreError``
           tagged with the first 3 argv elements
           for diagnosis;
        6. Any other exception: wrap in
           ``StoreError``.

        Args:
            args: argv after the binary.
            binary_path: explicit binary; defaults
                to ``self.cli_path``.
            timeout: in seconds (default 300 = 5
                min).
            env: extra env vars to merge over
                ``os.environ``.

        Returns:
            stdout as text.

        Raises:
            StoreError: missing binary, non-zero
                rc, timeout, or generic failure.
        """
        bin_path = binary_path or getattr(self, "cli_path", None)
        if not bin_path:
            raise StoreError(
                "CLI binary not found",
                store=self.store_name,
            )
        cmd = [bin_path] + args
        process_env = dict(os.environ) if env is None else {**os.environ, **env}

        def _run():
            """Inner sync subprocess runner (runs in a thread).

            Captures stdout+stderr as text, applies
            the timeout, raises ``StoreError`` on
            non-zero rc.

            Returns:
                stdout text.
            """
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=process_env,
            )
            if result.returncode != 0:
                raise StoreError(
                    f"CLI error (rc={result.returncode}): {result.stderr[:500]}",
                    store=self.store_name,
                )
            return result.stdout

        try:
            return await asyncio.to_thread(_run)
        except subprocess.TimeoutExpired as e:
            raise StoreError(
                f"CLI timeout after {timeout}s: {' '.join(cmd[:3])}",
                store=self.store_name,
            ) from e
        except StoreError:
            raise
        except Exception as e:
            raise StoreError(
                f"CLI execution failed: {e}",
                store=self.store_name,
            ) from e
