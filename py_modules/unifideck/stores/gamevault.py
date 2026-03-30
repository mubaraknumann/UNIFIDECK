"""
GameVault Store connector using direct REST API with Basic Auth (JWT tokens).

This module handles all GameVault operations including authentication,
library fetching, and game downloading from a self-hosted GameVault server.
"""
import asyncio
import base64
import json
import logging
import os
import ssl
import shutil
import tempfile
import time
import zipfile
from typing import Dict, Any, List, Optional

try:
    import aiohttp
except ImportError:
    aiohttp = None

from .base import Store, Game

logger = logging.getLogger(__name__)


class GameVaultConnector(Store):
    """Handles GameVault via direct REST API with JWT authentication"""

    def __init__(self, plugin_dir: Optional[str] = None, plugin_instance=None):
        self.plugin_dir = plugin_dir
        self.plugin_instance = plugin_instance
        self.token_file = os.path.expanduser("~/.config/unifideck/gamevault_token.json")

        self.server_url: Optional[str] = None
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.username: Optional[str] = None
        self.token_time: float = 0  # epoch when tokens were last saved/refreshed

        self._load_tokens()
        logger.info("[GameVault] Connector initialized")

    # ── Properties ──────────────────────────────────────────────────

    @property
    def store_name(self) -> str:
        return "gamevault"

    # ── Token management ────────────────────────────────────────────

    def _load_tokens(self):
        """Load stored auth tokens from disk"""
        try:
            if os.path.exists(self.token_file):
                with open(self.token_file, 'r') as f:
                    data = json.load(f)
                    self.server_url = data.get('server_url')
                    self.access_token = data.get('access_token')
                    self.refresh_token = data.get('refresh_token')
                    self.user_id = data.get('user_id')
                    self.username = data.get('username')
                    self.token_time = data.get('token_time', 0)
                    logger.info(f"[GameVault] Loaded tokens for {self.username}@{self.server_url}")
        except Exception as e:
            logger.error(f"[GameVault] Error loading tokens: {e}")

    def _save_tokens(self, server_url: str, access_token: str, refresh_token: str,
                     user_id: str = "", username: str = ""):
        """Save auth tokens to disk"""
        try:
            os.makedirs(os.path.dirname(self.token_file), exist_ok=True)
            now = time.time()
            with open(self.token_file, 'w') as f:
                json.dump({
                    'server_url': server_url,
                    'access_token': access_token,
                    'refresh_token': refresh_token,
                    'user_id': user_id,
                    'username': username,
                    'token_time': now,
                }, f)
            self.server_url = server_url
            self.access_token = access_token
            self.refresh_token = refresh_token
            self.user_id = user_id
            self.username = username
            self.token_time = now
            logger.info(f"[GameVault] Saved tokens for {username}@{server_url}")
        except Exception as e:
            logger.error(f"[GameVault] Error saving tokens: {e}")

    def _clear_tokens(self):
        """Clear tokens from memory and disk"""
        self.server_url = None
        self.access_token = None
        self.refresh_token = None
        self.user_id = None
        self.username = None
        self.token_time = 0
        try:
            if os.path.exists(self.token_file):
                os.remove(self.token_file)
                logger.info("[GameVault] Token file deleted")
        except Exception as e:
            logger.error(f"[GameVault] Error deleting token file: {e}")

    def _get_ssl_context(self) -> ssl.SSLContext:
        """Create SSL context with disabled cert verification (Steam Deck compatibility)"""
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _ensure_fresh_token(self) -> bool:
        """Proactively refresh token if older than 4 minutes (before 5-min expiry).

        GameVault access tokens expire in 5 minutes, so we refresh at 240 seconds.
        If refresh fails, we still allow the request to proceed — the API call
        handler in _api_json() will catch 401s and retry with a fresh token.
        """
        if not self.access_token or not self.refresh_token or not self.server_url:
            return False

        token_age = time.time() - self.token_time
        if token_age > 240:  # 4 minutes
            logger.info(f"[GameVault] Token is old ({token_age:.0f}s), refreshing proactively...")
            refreshed = await self._refresh_access_token()
            if not refreshed:
                logger.warning("[GameVault] Proactive refresh failed, will try existing token")
        return True

    async def _refresh_access_token(self) -> bool:
        """Refresh the access token using refresh token"""
        if not self.refresh_token or not self.server_url:
            return False

        try:
            ssl_context = self._get_ssl_context()
            timeout = aiohttp.ClientTimeout(total=10.0)
            connector = aiohttp.TCPConnector(ssl=ssl_context)

            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.post(
                    f"{self.server_url}/api/auth/refresh",
                    headers={"Authorization": f"Bearer {self.refresh_token}"}
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        self._save_tokens(
                            server_url=self.server_url,
                            access_token=data.get('access_token', ''),
                            refresh_token=data.get('refresh_token', self.refresh_token),
                            user_id=self.user_id or '',
                            username=self.username or '',
                        )
                        logger.info("[GameVault] Token refreshed successfully")
                        return True
                    else:
                        error_text = await response.text()
                        logger.warning(f"[GameVault] Token refresh failed ({response.status}): {error_text}")
                        return False
        except Exception as e:
            logger.error(f"[GameVault] Token refresh error: {e}")
            return False

    async def _api_request(self, method: str, path: str, timeout_seconds: float = 15.0, **kwargs) -> Optional[aiohttp.ClientResponse]:
        """Make an authenticated API request to the GameVault server.

        Returns the response object (caller must handle context manager) or None on failure.
        For simple requests, use _api_json() instead.
        """
        if not await self._ensure_fresh_token():
            return None

        ssl_context = self._get_ssl_context()
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        session = aiohttp.ClientSession(connector=connector, timeout=timeout)

        headers = kwargs.pop('headers', {})
        headers['Authorization'] = f'Bearer {self.access_token}'

        try:
            response = await session.request(method, f"{self.server_url}{path}", headers=headers, **kwargs)
            # Attach session to response so caller can close it
            response._unifideck_session = session
            return response
        except Exception as e:
            logger.error(f"[GameVault] API request failed: {method} {path}: {e}")
            await session.close()
            return None

    async def _api_json(self, method: str, path: str, timeout_seconds: float = 15.0, **kwargs) -> Optional[Dict]:
        """Make an authenticated API request and return JSON response"""
        if not await self._ensure_fresh_token():
            return None

        ssl_context = self._get_ssl_context()
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        connector = aiohttp.TCPConnector(ssl=ssl_context)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            headers = kwargs.pop('headers', {})
            headers['Authorization'] = f'Bearer {self.access_token}'

            try:
                async with session.request(method, f"{self.server_url}{path}", headers=headers, **kwargs) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status == 401:
                        logger.warning(f"[GameVault] 401 on {path}, attempting refresh")
                        if await self._refresh_access_token():
                            # Retry once with new token
                            headers['Authorization'] = f'Bearer {self.access_token}'
                            async with session.request(method, f"{self.server_url}{path}", headers=headers, **kwargs) as retry_resp:
                                if retry_resp.status == 200:
                                    return await retry_resp.json()
                        return None
                    else:
                        error_text = await response.text()
                        logger.warning(f"[GameVault] API {method} {path} failed ({response.status}): {error_text}")
                        return None
            except Exception as e:
                logger.error(f"[GameVault] API request error: {method} {path}: {e}")
                return None

    # ── Auth flow ───────────────────────────────────────────────────

    async def is_available(self) -> bool:
        """Check if GameVault is authenticated and server is reachable"""
        if not self.access_token or not self.server_url:
            logger.info("[GameVault] No credentials stored - not authenticated")
            return False

        try:
            data = await self._api_json('GET', '/api/users/me', timeout_seconds=5.0)
            if data is not None:
                logger.info("[GameVault] Status: Connected (authenticated)")
                return True
            else:
                logger.warning("[GameVault] Status: Not available (API check failed)")
                return False
        except asyncio.TimeoutError:
            logger.error("[GameVault] Status check timed out")
            return False
        except Exception as e:
            logger.error(f"[GameVault] Exception checking status: {e}")
            return False

    async def start_auth(self) -> Dict[str, Any]:
        """Signal frontend that GameVault uses credential-based auth (no URL popup)"""
        return {'success': True, 'auth_type': 'credentials'}

    async def complete_auth(self, auth_code: str) -> Dict[str, Any]:
        """Not used for GameVault — use login() instead"""
        return {'success': False, 'error': 'GameVault uses credential-based auth. Use gamevault_login instead.'}

    async def login(self, server_url: str, username: str, password: str) -> Dict[str, Any]:
        """Authenticate with GameVault using username/password (Basic Auth → JWT)

        Args:
            server_url: The GameVault server URL (e.g., https://gamevault.example.com)
            username: GameVault username
            password: GameVault password

        Returns:
            Dict with 'success' and optionally 'error'.
        """
        # Normalize server URL
        server_url = server_url.rstrip('/')
        if not server_url.startswith(('http://', 'https://')):
            server_url = f'https://{server_url}'

        logger.info(f"[GameVault] Attempting login for {username}@{server_url}")

        try:
            ssl_context = self._get_ssl_context()
            timeout = aiohttp.ClientTimeout(total=10.0)
            connector = aiohttp.TCPConnector(ssl=ssl_context)

            # Build Basic Auth header
            credentials = base64.b64encode(f"{username}:{password}".encode()).decode()

            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(
                    f"{server_url}/api/auth/basic/login",
                    headers={
                        'Authorization': f'Basic {credentials}',
                    }
                ) as response:
                    if response.status == 200 or response.status == 201:
                        data = await response.json()
                        self._save_tokens(
                            server_url=server_url,
                            access_token=data.get('access_token', ''),
                            refresh_token=data.get('refresh_token', ''),
                            user_id=str(data.get('id', '')),
                            username=username,
                        )
                        logger.info(f"[GameVault] Login successful for {username}")

                        # Trigger auto-sync if plugin instance available
                        if self.plugin_instance and hasattr(self.plugin_instance, 'sync_on_auth'):
                            try:
                                asyncio.create_task(self.plugin_instance.sync_on_auth("gamevault"))
                            except Exception as e:
                                logger.warning(f"[GameVault] Auto-sync trigger failed: {e}")

                        return {'success': True, 'message': f'Authenticated as {username}'}
                    elif response.status == 401:
                        logger.warning("[GameVault] Login failed: invalid credentials")
                        return {'success': False, 'error': 'Invalid username or password'}
                    else:
                        error_text = await response.text()
                        logger.warning(f"[GameVault] Login failed ({response.status}): {error_text}")
                        return {'success': False, 'error': f'Server error ({response.status})'}

        except aiohttp.ClientConnectorError as e:
            logger.error(f"[GameVault] Connection failed: {e}")
            return {'success': False, 'error': f'Could not connect to server: {server_url}'}
        except asyncio.TimeoutError:
            logger.error("[GameVault] Login timed out")
            return {'success': False, 'error': 'Connection timed out'}
        except Exception as e:
            logger.error(f"[GameVault] Login error: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    async def logout(self) -> Dict[str, Any]:
        """Logout from GameVault, clearing stored credentials"""
        try:
            # Best-effort token revocation
            if self.access_token and self.server_url:
                try:
                    await self._api_json('POST', '/api/auth/revoke',
                                         timeout_seconds=5.0,
                                         json={'refresh_token': self.refresh_token})
                except Exception as e:
                    logger.warning(f"[GameVault] Token revocation failed (non-critical): {e}")

            self._clear_tokens()
            logger.info("[GameVault] Logged out successfully")
            return {'success': True}
        except Exception as e:
            logger.error(f"[GameVault] Logout error: {e}")
            self._clear_tokens()
            return {'success': True}  # Still clear local state on error

    # ── Library ─────────────────────────────────────────────────────

    async def get_library(self) -> List[Game]:
        """Fetch user's game library from GameVault server"""
        if not self.access_token or not self.server_url:
            logger.warning("[GameVault] Cannot fetch library: not authenticated")
            return []

        logger.info("[GameVault] Fetching game library...")

        try:
            games = []
            page = 1
            per_page = 50

            while True:
                data = await self._api_json(
                    'GET', f'/api/games?page={page}&limit={per_page}',
                    timeout_seconds=15.0
                )

                if data is None:
                    logger.warning(f"[GameVault] Library fetch failed on page {page}")
                    break

                # GameVault API returns paginated results
                # Response format: {"data": [...], "meta": {"totalItems": N, ...}}
                game_list = data.get('data', data) if isinstance(data, dict) else data
                if isinstance(game_list, dict) and 'data' in game_list:
                    game_list = game_list['data']

                if not isinstance(game_list, list):
                    # If the response is a flat list (older API versions)
                    if isinstance(data, list):
                        game_list = data
                    else:
                        logger.warning(f"[GameVault] Unexpected response format: {type(data)}")
                        break

                for game_data in game_list:
                    game_id = str(game_data.get('id', ''))
                    title = game_data.get('title', '') or game_data.get('file_path', f'Game {game_id}')

                    # Build cover image URL from GameVault server
                    cover_image = None
                    box_image = game_data.get('box_image')
                    if box_image and isinstance(box_image, dict):
                        # box_image may be an object with an id
                        image_id = box_image.get('id')
                        if image_id:
                            cover_image = f"{self.server_url}/api/media/{image_id}"
                    elif box_image and isinstance(box_image, str):
                        cover_image = box_image if box_image.startswith('http') else f"{self.server_url}{box_image}"

                    games.append(Game(
                        id=game_id,
                        title=title,
                        store="gamevault",
                        is_installed=False,
                        cover_image=cover_image,
                        ownership_type="owned",
                    ))

                # Check pagination
                meta = data.get('meta', {}) if isinstance(data, dict) else {}
                total_items = meta.get('totalItems', 0)
                if not game_list or len(games) >= total_items or len(game_list) < per_page:
                    break
                page += 1

            logger.info(f"[GameVault] Found {len(games)} games")
            return games

        except Exception as e:
            logger.error(f"[GameVault] Error fetching library: {e}", exc_info=True)
            return []

    # ── Download / Install ──────────────────────────────────────────

    async def get_game_size(self, game_id: str) -> Optional[int]:
        """Get download size for a game in bytes"""
        data = await self._api_json('GET', f'/api/games/{game_id}', timeout_seconds=10.0)
        if data:
            return data.get('size')
        return None

    async def install_game(self, game_id: str, progress_callback=None, install_path: str = None) -> Dict[str, Any]:
        """Download and extract a game from GameVault server

        Args:
            game_id: GameVault game ID
            progress_callback: Async callback for progress updates
            install_path: Base directory for installation

        Returns:
            Dict with 'success', 'install_path', and optionally 'error'.
        """
        if not self.access_token or not self.server_url:
            return {'success': False, 'error': 'Not authenticated with GameVault'}

        # Ensure fresh token before long download
        await self._ensure_fresh_token()

        if not install_path:
            install_path = os.path.expanduser("~/Games")

        os.makedirs(install_path, exist_ok=True)

        # Get game metadata for title (folder naming)
        game_data = await self._api_json('GET', f'/api/games/{game_id}', timeout_seconds=10.0)
        if not game_data:
            return {'success': False, 'error': 'Failed to fetch game metadata'}

        game_title = game_data.get('title', f'gamevault_{game_id}')
        # Sanitize title for use as folder name
        safe_title = "".join(c for c in game_title if c.isalnum() or c in (' ', '-', '_', '.')).strip()
        if not safe_title:
            safe_title = f'gamevault_{game_id}'

        game_dir = os.path.join(install_path, safe_title)
        os.makedirs(game_dir, exist_ok=True)

        logger.info(f"[GameVault] Starting download of game {game_id} ({game_title}) to {game_dir}")

        # Report download phase
        if progress_callback:
            await progress_callback({'phase': 'downloading', 'phase_message': f'Downloading {game_title}...'})

        # Download the game archive
        temp_archive = None
        try:
            ssl_context = self._get_ssl_context()
            # Long timeout for large file downloads
            timeout = aiohttp.ClientTimeout(total=None, sock_read=300)
            connector = aiohttp.TCPConnector(ssl=ssl_context)

            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                headers = {'Authorization': f'Bearer {self.access_token}'}

                async with session.get(
                    f"{self.server_url}/api/games/{game_id}/download",
                    headers=headers
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"[GameVault] Download failed ({response.status}): {error_text}")
                        return {'success': False, 'error': f'Download failed ({response.status})'}

                    total_size = int(response.headers.get('Content-Length', 0))
                    downloaded = 0
                    start_time = time.time()

                    # Determine file extension from Content-Disposition or Content-Type
                    content_disp = response.headers.get('Content-Disposition', '')
                    if '.7z' in content_disp:
                        ext = '.7z'
                    elif '.rar' in content_disp:
                        ext = '.rar'
                    else:
                        ext = '.zip'

                    # Create temp file for download
                    temp_fd, temp_archive = tempfile.mkstemp(suffix=ext, prefix=f'gv_{game_id}_')
                    os.close(temp_fd)

                    with open(temp_archive, 'wb') as f:
                        async for chunk in response.content.iter_chunked(1024 * 1024):  # 1MB chunks
                            f.write(chunk)
                            downloaded += len(chunk)

                            if progress_callback and total_size > 0:
                                elapsed = time.time() - start_time
                                speed = downloaded / elapsed if elapsed > 0 else 0
                                eta = (total_size - downloaded) / speed if speed > 0 else 0
                                percent = (downloaded / total_size) * 100

                                await progress_callback({
                                    'progress_percent': round(percent, 1),
                                    'downloaded_bytes': downloaded,
                                    'total_bytes': total_size,
                                    'speed_mbps': round(speed / (1024 * 1024), 2),
                                    'eta_seconds': round(eta),
                                })

            logger.info(f"[GameVault] Download complete: {downloaded} bytes")

            # Report extraction phase
            if progress_callback:
                await progress_callback({'phase': 'extracting', 'phase_message': f'Extracting {game_title}...'})

            # Extract archive
            extracted = await self._extract_archive(temp_archive, game_dir)
            if not extracted:
                return {'success': False, 'error': 'Failed to extract game archive'}

            # Report completion
            if progress_callback:
                await progress_callback({'phase': 'complete', 'phase_message': 'Installation complete'})

            logger.info(f"[GameVault] Game {game_id} installed to {game_dir}")
            return {'success': True, 'install_path': game_dir}

        except asyncio.CancelledError:
            logger.info(f"[GameVault] Download cancelled for game {game_id}")
            raise
        except Exception as e:
            logger.error(f"[GameVault] Download error: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}
        finally:
            # Clean up temp file
            if temp_archive and os.path.exists(temp_archive):
                try:
                    os.remove(temp_archive)
                except Exception:
                    pass

    async def _extract_archive(self, archive_path: str, dest_dir: str) -> bool:
        """Extract a game archive to destination directory

        Handles zip files natively and falls back to shell commands for 7z/rar.
        """
        logger.info(f"[GameVault] Extracting {archive_path} to {dest_dir}")

        try:
            if archive_path.endswith('.zip'):
                # Native zip extraction
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._extract_zip, archive_path, dest_dir)
                return True
            else:
                # Use 7z command for .7z and .rar files
                cmd = ['7z', 'x', archive_path, f'-o{dest_dir}', '-y']
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()
                if process.returncode == 0:
                    return True
                else:
                    logger.error(f"[GameVault] 7z extraction failed: {stderr.decode()}")
                    return False

        except Exception as e:
            logger.error(f"[GameVault] Extraction error: {e}", exc_info=True)
            return False

    def _extract_zip(self, archive_path: str, dest_dir: str):
        """Synchronous zip extraction (run in executor)"""
        with zipfile.ZipFile(archive_path, 'r') as zf:
            zf.extractall(dest_dir)
