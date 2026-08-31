"""GameVault store — Layer-4 ``StoreBase`` implementation.

``GameVaultStore`` is the orchestration class that wires:

* :class:`GameVaultAuth`           — JWT-based HTTP Basic auth.
* :class:`GameVaultLibraryReader`  — paginated game list.
* :class:`GameVaultInstaller`      — download / extract pipeline.

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
    Events,
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

    # NOTE: no per-store ``sync_timeout``. Nothing reads one — the sync
    # applies ``PER_STORE_FETCH_TIMEOUT_SECONDS`` (120s) to every store
    # alike — so a field here would be a write-only declaration of the kind
    # audit §3.1 removed. If 120s proves too tight for a large self-hosted
    # library, that is a change to the shared constant with a measurement
    # behind it, not a silent per-store number.

    # No ``uses_wine`` / ``supports_cloud_saves`` here: both were removed
    # from StoreInfo (audit §3.1, register 26/31) and are derived instead —
    # ``client_runs_in_prefix`` from ``WRAPPER_STORES``, the capability flags
    # from ``core.store_capabilities``. Passing either raises TypeError, by
    # design. GameVault is in none of those sets: it is not a wrapper store,
    # and it has no achievements, cloud saves, language picker or browser
    # storefront.
    store_info = StoreInfo(
        name="gamevault",
        display_name="GameVault",
        auth_method="manual",
        icon_asset="gamevault.png",
        supports_install=True,
    )

    def __init__(
        self,
        bus: EventBus,
        cache: CacheManager,
        plugin_dir: str | None = None,
        config: ConfigManager | None = None,
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
        except Exception:
            # ``None``, never ``[]``. The sync treats an empty list as a real
            # answer ("this user owns nothing") and the shortcut reconcile
            # sweeps accordingly, so returning one here would delete the
            # user's GameVault shortcuts every time the server was down.
            logger.exception("[GameVaultStore] get_library failed")
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
        """Remove the game, then announce it like every other store does.

        ``GAME_UNINSTALLED`` is what ``ShortcutService`` subscribes to in
        order to flip the shortcut back to "not installed" while keeping it
        (and its appid, artwork and playtime) in place. Emitting it here —
        rather than having the uninstall RPC call ``mark_uninstalled``
        directly — keeps GameVault on the same path as GOG, Epic, Amazon,
        Ubisoft and Battle.net, instead of adding a second mechanism that
        would fire twice for all of them.
        """
        result = await self._installer.uninstall_game(game_id)
        if result.success:
            await self._emit(
                Events.GAME_UNINSTALLED,
                store="gamevault",
                game_id=game_id,
            )
        return result

    async def update_game(self, game_id: str, **kwargs: Any) -> InstallResult:
        """Re-download the game (GameVault has no delta updates)."""
        return await self.install_game(game_id, **kwargs)

    async def check_for_updates(self) -> list[str]:
        """GameVault does not expose a server-side update API."""
        return []

    async def get_installed_path(self, game_id: str) -> str | None:
        """Install dir per our own marker, or ``None`` if not installed.

        The hook every store implements. It matters more here than
        elsewhere: it is what lets Change Executable resolve a directory when
        the games.map row is missing, which for this store is the situation
        the picker exists for.
        """
        info = self._installer.get_install_info(game_id)
        path = (info or {}).get("install_path")
        return path if isinstance(path, str) and path else None

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
