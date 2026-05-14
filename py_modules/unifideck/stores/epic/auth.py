"""auth.py — Epic Games OAuth via legendary subprocess.

# OP-48b | py_modules/unifideck/stores/epic/auth.py | Depends: OP-47b

Legendary handles the OAuth handshake itself — we just spawn it, scrape
the auth URL from its stdout, hand the URL to the
:class:`AuthOrchestrator` (which drives a CDP-instrumented Edge
browser), and finally call ``legendary auth --code <code>`` to mint
tokens once the orchestrator delivers the redirect code.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ...auth.orchestrator import AuthOrchestrator
from ...core.types import AuthResult, Events, Result, StoreAuthError
from ...event_bus.event_bus import EventBus
from ...security import audit_auth_flow

logger = logging.getLogger(__name__)
_EPIC_REDIRECT_URIS: list[str] = [
    'https://legendary.epicgames.com/callback',
    'https://www.epicgames.com/id/api/redirect',
]
_AUTH_URL_MARKERS = ('epicgames.com',)


class EpicAuthFlow:
    """Epic Games OAuth flow driven by the ``legendary`` CLI.

    Spawns ``legendary auth``, scrapes the login URL out of
    its stdout, hands the URL to the AuthOrchestrator for the
    browser leg, then mints tokens by calling
    ``legendary auth --code <code>`` once the orchestrator
    delivers the OAuth redirect code.
    """

    def __init__(
        self,
        bus: EventBus,
        orchestrator: AuthOrchestrator,
        cli_path: str | None,
        cli_timeout_seconds: int = 30,
    ) -> None:
        """Wire dependencies for the Legendary CLI-driven Epic auth flow.

        Args:
            bus: Event bus.
            orchestrator: Auth orchestrator.
            cli_path: Path to the bundled ``legendary`` binary.
            cli_timeout_seconds: Hard timeout for the CLI call.
        """
        self._bus = bus
        self._orchestrator = orchestrator
        self._cli_path = cli_path
        self._cli_timeout_seconds = cli_timeout_seconds

    @audit_auth_flow(store='epic', method='oauth_cli')
    async def start_auth(self) -> AuthResult:
        """Kick off the Epic OAuth flow.

        Steps: spawn legendary auth → scrape the login URL out of
        its stdout (with timeout) → terminate legendary → hand the
        URL to the orchestrator with ``_register_code`` as the
        completion callback.

        Returns:
            ``AuthResult`` — ``success=True`` only means the browser
            was launched; actual token minting happens later in
            ``_register_code``.
        """
        if not self._cli_path:
            return AuthResult(
                success=False, store='epic', error='legendary_not_found',
            )
        try:
            url = await self._fetch_login_url()
        except StoreAuthError as e:
            return AuthResult(
                success=False, store='epic', error=str(e),
            )
        if not url:
            return AuthResult(
                success=False, store='epic', error='auth_url_not_found',
            )
        await self._orchestrator.start_browser_auth(
            url=url,
            allowed_redirect_uris=_EPIC_REDIRECT_URIS,
            cookie_domain='epicgames.com',
            on_code=self._register_code,
            store='epic',
        )
        return AuthResult(success=True, store='epic', redirect_url=url)

    async def logout(self) -> Result:
        """Invoke ``legendary auth --delete`` and emit STORE_LOGOUT.

        Returns:
            ``Result`` — ``success=False`` if legendary is missing
            or the subprocess can't be spawned.
        """
        if not self._cli_path:
            return Result(success=False, error='legendary_not_found')
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli_path, 'auth', '--delete',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except OSError as e:
            return Result(success=False, error=f'spawn_failed:{e}')
        await self._bus.emit(Events.STORE_LOGOUT, store='epic')
        return Result(success=True)

    async def _fetch_login_url(self) -> str:
        """Spawn legendary, scrape the auth URL, and terminate it.

        Wraps the scrape in an asyncio timeout. The temporary
        subprocess is always terminated, even on timeout.

        Returns:
            The login URL string (empty if not found).

        Raises:
            StoreAuthError: scrape timed out.
        """
        proc = await self._spawn_legendary_auth()
        try:
            url = await asyncio.wait_for(
                self._scrape_url_from_proc(proc),
                timeout=self._cli_timeout_seconds,
            )
        except TimeoutError:
            await self._terminate_legendary(proc)
            raise StoreAuthError(
                'auth_url_scrape_timeout', store='epic',
            ) from None
        await self._terminate_legendary(proc)
        return url or ''

    async def _spawn_legendary_auth(self) -> Any:
        """Spawn ``legendary auth`` with stdout piped and stderr merged.

        Returns:
            The live subprocess handle.
        """
        return await asyncio.create_subprocess_exec(
            self._cli_path, 'auth',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    async def _scrape_url_from_proc(self, proc: Any) -> str | None:
        """Read legendary stdout line-by-line until an auth URL appears.

        Args:
            proc: Live legendary subprocess (stdout piped).

        Returns:
            URL string on first match, or ``None`` on EOF / no
            stdout stream.
        """
        if proc.stdout is None:
            return None
        while True:
            line = await proc.stdout.readline()
            if not line:
                return None
            decoded = line.decode('utf-8', errors='replace')
            url = self._extract_url(decoded)
            if url:
                return url

    @staticmethod
    async def _terminate_legendary(proc: Any) -> None:
        """Terminate the legendary auth subprocess (best-effort).

        Sends SIGTERM, waits 5s for the process to exit, then
        falls back to SIGKILL. Process-already-gone errors are
        swallowed.

        Args:
            proc: Subprocess handle (no-op if already exited).
        """
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def _register_code(self, code: str) -> AuthResult:
        """Feed the captured OAuth code to ``legendary auth --code``.

        Invoked as the AuthOrchestrator's completion callback when
        the user lands on the Epic redirect URL. Emits
        STORE_AUTH_COMPLETE / STORE_AUTH_FAILED accordingly.

        Args:
            code: OAuth code captured from the redirect URL.

        Returns:
            ``AuthResult``.
        """
        if not code:
            return AuthResult(
                success=False, store='epic', error='no_auth_code',
            )
        if not self._cli_path:
            return AuthResult(
                success=False, store='epic', error='legendary_not_found',
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli_path, 'auth', '--code', code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _out, err = await proc.communicate()
        except OSError as e:
            await self._bus.emit(
                Events.STORE_AUTH_FAILED, store='epic', error=f'spawn:{e}',
            )
            return AuthResult(
                success=False, store='epic', error=f'spawn:{e}',
            )
        if proc.returncode != 0:
            message = err.decode('utf-8', errors='replace').strip() or 'auth_failed'
            await self._bus.emit(
                Events.STORE_AUTH_FAILED, store='epic', error=message,
            )
            return AuthResult(
                success=False, store='epic', error=message,
            )
        await self._bus.emit(Events.STORE_AUTH_COMPLETE, store='epic')
        return AuthResult(success=True, store='epic')

    @staticmethod
    def _extract_url(line: str) -> str | None:
        """Extract the first ``https://...epicgames.com...`` URL from one line.

        Args:
            line: One line of legendary stdout.

        Returns:
            The matched URL (with trailing ``)``, ``,``, ``.``
            stripped), or ``None`` if no match.
        """
        if 'https://' not in line:
            return None
        for word in line.split():
            if not word.startswith('https://'):
                continue
            if any(marker in word.lower() for marker in _AUTH_URL_MARKERS):
                return word.rstrip(').,')
        return None
