"""Download RPC mixin for Plugin class.

OP-26c | rpc/mixins/download.py
"""
from __future__ import annotations

from typing import Any

from unifideck.rpc.errors import RpcError


class DownloadRPCMixin:
    """Game install/uninstall, download queue, and storage locations."""

    registry: Any
    services: Any

    def _require_store(self, store: str) -> Any:
        """Return store adapter or raise ``store_not_found``."""
        adapter = self.registry.get(store)
        if adapter is None:
            raise RpcError("store_not_found", store=store)
        return adapter

    def _require_download(self) -> Any:
        """Return download service or raise ``service_unavailable``."""
        svc = getattr(self.services, "download", None)
        if svc is None:
            raise RpcError("service_unavailable", service="download")
        return svc

    async def install_game(self, store: str, game_id: str, **kw: Any) -> Any:
        """Install a game via the responsible store adapter.

        Args:
            store: Store identifier.
            game_id: Per-store game identifier.
            **kw: Store-specific install options forwarded verbatim.

        Returns:
            The adapter's install result.

        Raises:
            RpcError: ``store_not_found`` when no adapter is
                registered for ``store``.
        """
        return await self._require_store(store).install(game_id, **kw)

    async def uninstall_game(self, store: str, game_id: str) -> Any:
        """Uninstall a game via the responsible store adapter.

        Args:
            store: Store identifier.
            game_id: Per-store game identifier.

        Returns:
            The adapter's uninstall result.

        Raises:
            RpcError: ``store_not_found`` when no adapter is
                registered for ``store``.
        """
        return await self._require_store(store).uninstall(game_id)

    async def check_game_update(self, store: str, game_id: str) -> Any:
        """Check whether a specific game has an update available."""
        return await self._require_store(store).check_update(game_id)

    async def cancel_download(self, download_id: str) -> Any:
        """Cancel an in-progress download.

        Args:
            download_id: Identifier returned at enqueue time.

        Returns:
            Whatever the download service returns from ``cancel``
            (no-op if the id is unknown).

        Raises:
            RpcError: ``service_unavailable`` when the download
                service isn't wired.
        """
        return await self._require_download().cancel(download_id)

    async def get_download_queue(self) -> Any:
        """Return the current download queue."""
        return await self._require_download().get_queue()

    async def get_storage_locations(self) -> Any:
        """Return available storage locations."""
        storage = getattr(self.services, "storage", None)
        if storage is None:
            return []
        return await storage.get_locations()
