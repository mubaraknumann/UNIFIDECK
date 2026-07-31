"""GameVault store — Layer-4 ``StoreBase`` implementation.

``GameVaultStore`` is the orchestration class that wires:

* :class:`GameVaultAuth`           (OP-GV-a) — JWT-based HTTP Basic auth.
* :class:`GameVaultLibraryReader`  (OP-GV-b) — paginated game list.
* :class:`GameVaultInstaller`      (OP-GV-c) — download / extract pipeline.

GameVault is a self-hosted game server; all network calls go to the
server URL stored in the on-disk config file.

Config section (``defaults/config.json`` → ``stores.gamevault``):
    config_file:          path to the persisted credentials/token JSON
    default_install_root: default directory for extracted games
    download_dir:         *separate* temp directory for archive downloads
                          (archive is deleted after successful extraction)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from unifideck.core.types import (
    AuthResult,
    Game,
    InstallResult,
    Result,
    StoreInfo,
)
from unifideck.stores.shared.store_base import StoreBase

from .auth import GameVaultAuth
from .install import GameVaultInstaller, ProgressCallback
from .library import GameVaultLibraryReader

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_FILE = "~/.local/share/unifideck/gamevault_config.json"
_DEFAULT_INSTALL_ROOT = "~/Games/GameVault"
_DEFAULT_DOWNLOAD_DIR = "~/.local/share/unifideck/gamevault_downloads"


class GameVaultStore(StoreBase):
    """GameVault self-hosted game server connector."""

    # GameVault serves large libraries over HTTP — needs more headroom than the default 120s.
    sync_timeout = 300

    store_info = StoreInfo(
        name="gamevault",
        display_name="GameVault",
        auth_method="manual",
        icon_asset="gamevault.png",
        uses_wine=False,
        supports_install=True,
        supports_cloud_saves=False,
    )

    def __init__(
        self,
        bus: "EventBus",
        cache: "CacheManager",
        plugin_dir: str | None = None,
        config: "ConfigManager | None" = None,
    ) -> None:
        """Initialise the GameVault store connector."""
        super().__init__(bus, cache, plugin_dir, config)

        gv_cfg = (config.get("stores.gamevault") if config else None) or {}

        config_file = gv_cfg.get("config_file", _DEFAULT_CONFIG_FILE)
        default_install_root = gv_cfg.get("default_install_root", _DEFAULT_INSTALL_ROOT)
        download_dir = gv_cfg.get("download_dir", _DEFAULT_DOWNLOAD_DIR)

        self._auth = GameVaultAuth(config_file=config_file)
        self._installer = GameVaultInstaller(
            default_install_root=default_install_root,
            download_dir=download_dir,
        )
        self._library_reader = GameVaultLibraryReader(installer=self._installer)

    # ── StoreBase abstract methods ──────────────────────────────────

    async def is_available(self) -> bool:
        """True when credentials are stored (server reachability checked lazily at sync time)."""
        return self._auth.is_authenticated()

    async def start_auth(self, **kwargs: Any) -> AuthResult:
        """Authenticate with ``server_url``, ``username``, ``password``,
        ``verify_ssl``, and optionally ``download_dir``."""
        return await self._auth.start_auth(
            server_url=kwargs.get("server_url", ""),
            username=kwargs.get("username", ""),
            password=kwargs.get("password", ""),
            verify_ssl=kwargs.get("verify_ssl", True),
            download_dir=kwargs.get("download_dir") or None,
        )

    async def complete_auth(self, **kwargs: Any) -> AuthResult:
        """GameVault uses a single-step auth; returns cached auth state."""
        if self._auth.is_authenticated():
            return AuthResult(
                success=True,
                action="authenticated",
                tokens_cached=True,
                store="gamevault",
            )
        return AuthResult(
            success=False,
            error="Not authenticated — call start_auth first",
            store="gamevault",
        )

    async def logout(self) -> Result:
        return await self._auth.logout()

    async def get_library(self, *, force: bool = False) -> list[Game] | None:
        headers = await self._auth.get_auth_headers()
        if not headers:
            logger.warning("[GameVaultStore] Not authenticated, library unavailable")
            return None
        server_url = self._auth.server_url or ""
        try:
            return await self._library_reader.get_library(
                server_url=server_url,
                auth_headers=headers,
                verify_ssl=self._auth.verify_ssl,
                force=force,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[GameVaultStore] get_library failed: %s", exc)
            return None

    async def install_game(
        self,
        game_id: str,
        base_path: str | None = None,
        progress_cb: ProgressCallback | None = None,
        **kwargs: Any,
    ) -> InstallResult:
        headers = await self._auth.get_auth_headers()
        if not headers:
            return InstallResult(
                success=False,
                error="Not authenticated",
                store="gamevault",
                game_id=game_id,
            )
        install_path: str | None = base_path or kwargs.get("install_path")
        progress_callback: ProgressCallback | None = progress_cb or kwargs.get("progress_callback")
        # per-install override → saved credential setting → installer default
        download_dir: str | None = kwargs.get("download_dir") or self._auth.download_dir or None
        server_url = self._auth.server_url or ""

        return await self._installer.install_game(
            game_id,
            auth_headers=headers,
            server_url=server_url,
            verify_ssl=self._auth.verify_ssl,
            install_path=install_path,
            progress_callback=progress_callback,
            download_dir=download_dir,
        )

    async def uninstall_game(self, game_id: str, **kwargs: Any) -> Result:
        return await self._installer.uninstall_game(game_id)

    async def update_game(self, game_id: str, **kwargs: Any) -> InstallResult:
        """Re-download the game (GameVault has no delta updates)."""
        return await self.install_game(game_id, **kwargs)

    async def check_for_updates(self) -> list[str]:
        """GameVault does not expose a server-side update API."""
        return []

    async def get_game_size(self, game_id: str) -> int | None:
        headers = await self._auth.get_auth_headers()
        if not headers:
            return None
        server_url = self._auth.server_url or ""
        return await self._installer.get_game_size(
            game_id,
            auth_headers=headers,
            server_url=server_url,
            verify_ssl=self._auth.verify_ssl,
        )

    # ── Extra helpers called by main.py (backward-compat surface) ──

    def _get_install_info(self, game_id: str) -> dict[str, Any] | None:
        """Return the persisted install marker dict for *game_id*."""
        return self._installer.get_install_info(game_id)

    async def get_installed(self) -> dict[str, dict[str, Any]]:
        """Return {game_id: install_info} for all installed GameVault games."""
        return self._installer.get_installed()
