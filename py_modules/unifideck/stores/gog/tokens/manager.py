"""High-level GOG token manager — single public surface for the rest of the store.

OP-22-gog-tokens-manager | py_modules/unifideck/stores/gog/tokens/manager.py

Coordinates the three internal helpers
(``_TokenStorage``, ``_TokenOAuth``,
``_GogdlCreds``) behind a single class so the rest
of the GOG store doesn't have to know about
storage / OAuth / gogdl integration details.

Holds the current ``access_token``,
``refresh_token``, and ``user_info`` as instance
state. These are mutated atomically on
``load`` / ``save`` / ``clear``.

The ``gogdl_credentials`` async context manager is
the public way to spawn gogdl subprocesses — it
materialises a tempdir + creds file at enter and
cleans up at exit.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from ....security import SecureTokenStore
from .gogdl_credentials import _GogdlCreds
from .oauth import _TokenOAuth
from .storage import _TokenStorage
from .user_info import GOGUserInfo

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from ..config import GOGConfig

logger = logging.getLogger(__name__)


class GOGTokenManager:
    """Public façade over the GOG token subsystem.

    Single point of entry for the rest of the
    plugin — auth flow, library reader, install
    pipeline all go through this class.

    Composes the three internal helpers via
    dependency injection so each one is
    individually testable.
    """

    def __init__(
        self,
        config: GOGConfig,
        secure_store: SecureTokenStore | None = None,
        bus: Any = None,
    ) -> None:
        """Build the manager and its internal helpers.

        ``secure_store`` defaults to a freshly-
        constructed ``SecureTokenStore`` if not
        provided; the optional arg lets tests
        inject a fake.

        The internal helpers reference back into
        this instance through ``save_callback=self.save``
        — keeps persistence centralised even when
        the OAuth helper handles the actual HTTP.

        Args:
            config: ``GOGConfig``.
            secure_store: encryption helper
                (test-injection).
            bus: event bus (for audit events).
        """
        self._config = config
        self._bus = bus
        self._secure_store = secure_store or SecureTokenStore(
            bus=bus,
        )
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._user_info = GOGUserInfo()
        self._storage = _TokenStorage(
            config=config,
            bus=bus,
            secure_store=self._secure_store,
        )
        self._oauth = _TokenOAuth(
            config=config,
            save_callback=self.save,
        )
        self._gogdl = _GogdlCreds(config=config)

    @property
    def access_token(self) -> str | None:
        """Current access token, or ``None`` if not signed in.

        Returns:
            Token string or ``None``.
        """
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        """Current refresh token, or ``None`` if not signed in.

        Returns:
            Token string or ``None``.
        """
        return self._refresh_token

    @property
    def user_info(self) -> GOGUserInfo:
        """Current ``GOGUserInfo`` (empty values when not signed in).

        Returns:
            ``GOGUserInfo``.
        """
        return self._user_info

    @property
    def has_tokens(self) -> bool:
        """Quick check: both access + refresh tokens present.

        Returns:
            True iff both tokens are truthy
            strings.
        """
        return bool(
            self._access_token and self._refresh_token,
        )

    def get_token_age_seconds(self) -> float:
        """Compute the token file's age in seconds (used for refresh decisions).

        Missing file or stat error → return
        ``+inf`` so any threshold comparison
        triggers a refresh (which will then fail
        if there's no refresh token, going down
        the "session is dead" path).

        Returns:
            Age in seconds, or ``+inf``.
        """
        path = os.path.expanduser(self._config.token_file)
        if not os.path.isfile(path):
            return float("inf")
        try:
            return time.time() - os.path.getmtime(path)
        except OSError:
            return float("inf")

    async def load(self) -> bool:
        """Read tokens from disk into instance state.

        Three-step:

        1. Delegate to ``_storage.load``;
        2. None result → return False (no tokens
           on disk, normal first-launch);
        3. Otherwise unpack into instance state
           and return True.

        Returns:
            True iff tokens loaded.
        """
        result = await self._storage.load()
        if result is None:
            return False
        access, refresh, user_info = result
        self._access_token = access
        self._refresh_token = refresh
        self._user_info = user_info
        return True

    async def save(
        self,
        access_token: str,
        refresh_token: str,
    ) -> bool:
        """Persist new tokens — also refreshes user info from /userData.json.

        Order matters:

        1. Fetch fresh user info using the new
           access token (best-effort, falls back
           to existing if endpoint fails);
        2. Persist all three to disk
           (encrypted);
        3. On successful persist, update
           instance state.

        Mutating instance state only on persist
        success keeps RAM + disk in lock-step.

        Args:
            access_token: new bearer.
            refresh_token: new refresh.

        Returns:
            True on successful persist.
        """
        new_user_info = await self._oauth.fetch_user_info(
            access_token,
            self._user_info,
        )
        ok = await self._storage.persist(
            access_token,
            refresh_token,
            new_user_info,
        )
        if not ok:
            return False
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._user_info = new_user_info
        return True

    async def clear(self) -> None:
        """Wipe in-memory tokens and remove on-disk files.

        Used by logout. After this call,
        ``has_tokens`` returns False and a
        subsequent ``load`` will fail until the
        next OAuth flow completes.
        """
        self._access_token = None
        self._refresh_token = None
        self._user_info = GOGUserInfo()
        await self._storage.clear_files()

    async def exchange_code(self, auth_code: str) -> bool:
        """Forward to the OAuth helper.

        Convenience proxy — callers like
        ``GOGBrowserAuth`` don't need to know
        about ``_TokenOAuth``.

        Args:
            auth_code: OAuth code.

        Returns:
            True on success.
        """
        return await self._oauth.exchange_code(auth_code)

    async def refresh_if_stale(self) -> bool:
        """Refresh tokens if older than the configured threshold.

        Passes the current in-memory tokens + age
        to the OAuth helper, which decides
        whether to actually refresh.

        Returns:
            True iff after this call the access
            token is fresh.
        """
        return await self._oauth.refresh_if_stale(
            access_token=self._access_token,
            refresh_token=self._refresh_token,
            age_seconds=self.get_token_age_seconds(),
        )

    @contextlib.asynccontextmanager
    async def gogdl_credentials(
        self,
    ) -> AsyncIterator[dict[str, str]]:
        """Context manager yielding a subprocess env with GOGDL_CONFIG_PATH wired up.

        Wraps ``acquire_gogdl_creds`` + the
        cleanup callable in a try/finally so
        callers can ``async with`` the manager
        and not worry about leaking the tempdir.

        Yields:
            Subprocess env dict ready to pass
            to ``asyncio.create_subprocess_exec``.
        """
        env, cleanup = await self.acquire_gogdl_creds()
        try:
            yield env
        finally:
            await cleanup()

    async def acquire_gogdl_creds(
        self,
    ) -> tuple[
        dict[str, str],
        Any,
    ]:
        """Lower-level acquire — return ``(env, cleanup_callable)``.

        Most callers should use the
        ``gogdl_credentials`` context manager
        instead; this method exists for callers
        that need to detach the cleanup from a
        single block (e.g. background processes).

        Raises ``RuntimeError`` if not signed in —
        that's a programming error, the auth flow
        should guarantee tokens before any gogdl
        invocation.

        Returns:
            ``(env, cleanup)`` pair.

        Raises:
            RuntimeError: tokens are missing.
        """
        if not self._access_token or not self._refresh_token:
            raise RuntimeError(
                "acquire_gogdl_creds called without authenticated tokens",
            )
        return await self._gogdl.acquire(
            self._access_token,
            self._refresh_token,
        )
