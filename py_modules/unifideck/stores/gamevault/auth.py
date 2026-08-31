"""GameVault authentication — JWT-based HTTP Basic flow.

Persists credentials and tokens to a JSON config file on disk so
the server_url / username / password / access_token survive restarts.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

from unifideck.core.types import AuthResult, Result

logger = logging.getLogger(__name__)

_CONFIG_PATH_DEFAULT = "~/.local/share/unifideck/gamevault_config.json"


class GameVaultAuth:
    """JWT-based authentication for a self-hosted GameVault server.

    Stores all state in a plain JSON file so tokens survive plugin
    restarts.  The credentials (username/password) are kept so the
    access token can be refreshed transparently.
    """

    def __init__(self, config_file: str = _CONFIG_PATH_DEFAULT) -> None:
        self._config_path = Path(config_file).expanduser()
        self._cfg: dict[str, Any] = {}
        self._load_config()

    # ── Persistence ────────────────────────────────────────────────

    def _load_config(self) -> None:
        try:
            if self._config_path.exists():
                self._cfg = json.loads(self._config_path.read_text())
        except Exception as exc:
            logger.warning("[GameVaultAuth] Could not read config: %s", exc)
            self._cfg = {}

    def _save_config(self) -> None:
        """Persist credentials + token, owner-readable only.

        This file holds the user's GameVault password in clear text — the
        server issues short-lived JWTs and offers no refresh grant we can
        use, so the credentials are what re-authenticates unattended. Mode
        ``0600`` is the same bar the Epic/Amazon CLI credential files are
        held to (register 19; encryption at rest is the open decision for
        all three, not a GameVault-specific gap). ``chmod`` is applied after
        the write and also to a file that already existed, since
        ``write_text`` leaves the mode of an existing file untouched.
        """
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            self._config_path.write_text(json.dumps(self._cfg, indent=2))
            self._config_path.chmod(0o600)
        except Exception:
            logger.exception("[GameVaultAuth] Could not save config")

    def _clear_config(self) -> None:
        self._cfg = {}
        try:
            if self._config_path.exists():
                self._config_path.unlink()
        except Exception as exc:
            logger.warning("[GameVaultAuth] Could not delete config: %s", exc)

    # ── Token helpers ───────────────────────────────────────────────

    @staticmethod
    def _parse_jwt_expiry(token: str) -> float | None:
        """Return POSIX expiry timestamp from a JWT, or None."""
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None
            # Add padding so b64decode doesn't choke
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
            return float(payload.get("exp", 0)) or None
        except Exception:
            return None

    def _is_token_valid(self, margin_seconds: int = 60) -> bool:
        """True if the stored access token has not expired yet."""
        token = self._cfg.get("access_token", "")
        if not token:
            return False
        expiry = self._parse_jwt_expiry(token)
        if expiry is None:
            return False
        return time.time() < expiry - margin_seconds

    # ── Public helpers (used by store.py) ───────────────────────────

    @property
    def server_url(self) -> str | None:
        return self._cfg.get("server_url")

    @property
    def verify_ssl(self) -> bool:
        return bool(self._cfg.get("verify_ssl", True))

    @property
    def download_dir(self) -> str | None:
        return self._cfg.get("download_dir") or None

    def is_authenticated(self) -> bool:
        if not self._cfg.get("server_url"):
            return False
        if self._is_token_valid():
            return True
        # Token expired but credentials are stored — treat as connected;
        # get_auth_headers() will refresh transparently on the next call.
        return bool(self._cfg.get("username") and self._cfg.get("password"))

    async def get_auth_headers(self) -> dict[str, str] | None:
        """Return Bearer headers, refreshing the token if needed."""
        if self._is_token_valid():
            return {"Authorization": f"Bearer {self._cfg['access_token']}"}
        if await self._relogin():
            return {"Authorization": f"Bearer {self._cfg['access_token']}"}
        return None

    # ── Re-authentication ───────────────────────────────────────────

    async def _relogin(self) -> bool:
        """Sign in again with the stored credentials.

        Deliberately *not* named after the shared Epic helper that refreshes
        an existing grant. GameVault issues no refresh token, so there is
        nothing to refresh — the only way to get a new JWT is to present the
        username and password again. Different operation, different name.
        """
        username = self._cfg.get("username", "")
        password = self._cfg.get("password", "")
        server_url = self._cfg.get("server_url", "")
        if not all([username, password, server_url]):
            return False
        result = await self._do_login(server_url, username, password, self.verify_ssl)
        return result.success

    # ── Auth flow ───────────────────────────────────────────────────

    async def start_auth(
        self,
        *,
        server_url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
        download_dir: str | None = None,
    ) -> AuthResult:
        """Authenticate against the GameVault server and persist the JWT.

        If *download_dir* is provided it is stored in the config file so
        the installer can pick it up after a restart.
        """
        result = await self._do_login(server_url, username, password, verify_ssl)
        if result.success:
            update: dict[str, Any] = {
                "server_url": server_url.rstrip("/"),
                "username": username,
                "password": password,
                "verify_ssl": verify_ssl,
            }
            if download_dir is not None:
                update["download_dir"] = download_dir
            self._cfg.update(update)
            self._save_config()
        return result

    async def _do_login(
        self,
        server_url: str,
        username: str,
        password: str,
        verify_ssl: bool,
    ) -> AuthResult:
        url = f"{server_url.rstrip('/')}/api/auth/basic/login"
        # Log every outcome below. A sign-in that fails is the thing a user
        # reports, and the first field report of this store produced not one
        # line in the plugin log to work from.
        logger.info("[GameVaultAuth] login POST %s (verify_ssl=%s)", url, verify_ssl)
        try:
            import aiohttp
            connector = aiohttp.TCPConnector(ssl=verify_ssl)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    url,
                    auth=aiohttp.BasicAuth(username, password),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 401:
                        logger.warning(
                            "[GameVaultAuth] login rejected (HTTP 401) for %s", url,
                        )
                        return AuthResult(
                            success=False,
                            error="Invalid username or password",
                            store="gamevault",
                        )
                    if resp.status != 200:
                        logger.warning(
                            "[GameVaultAuth] login failed: HTTP %s from %s",
                            resp.status, url,
                        )
                        return AuthResult(
                            success=False,
                            error=f"Server returned HTTP {resp.status}",
                            store="gamevault",
                        )
                    data = await resp.json()
        except ImportError:
            return AuthResult(
                success=False,
                error="aiohttp not available — Python deps not vendored",
                store="gamevault",
            )
        except Exception as exc:
            return AuthResult(
                success=False,
                error=str(exc),
                store="gamevault",
            )

        token = data.get("access_token") or data.get("token", "")
        if not token:
            logger.warning(
                "[GameVaultAuth] login returned no token; response keys=%s",
                sorted(data) if isinstance(data, dict) else type(data).__name__,
            )
            return AuthResult(
                success=False,
                error="Server response contained no token",
                store="gamevault",
            )
        self._cfg["access_token"] = token
        self._cfg["token_expiry"] = self._parse_jwt_expiry(token) or 0
        logger.info("[GameVaultAuth] login OK, token cached")
        return AuthResult(
            success=True,
            action="authenticated",
            tokens_cached=True,
            store="gamevault",
        )

    async def logout(self) -> Result:
        self._clear_config()
        return Result(success=True, store="gamevault")
