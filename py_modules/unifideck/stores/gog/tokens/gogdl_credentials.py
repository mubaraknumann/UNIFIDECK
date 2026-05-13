"""Build a temporary gogdl credentials file + GOGDL_CONFIG_PATH for subprocess use.

OP-22-gog-tokens-gogdlcreds
File: py_modules/unifideck/stores/gog/tokens/gogdl_credentials.py

gogdl (the GOG CLI) reads its OAuth credentials
from ``<config_dir>/gog_credentials.json``. Rather
than maintaining a persistent file in the user's
real gogdl config dir (which would leak tokens
through the whole machine's gogdl), we materialise
a one-shot tempdir for each subprocess invocation.

The flow:

1. ``acquire(access, refresh)`` creates a tempdir,
   writes the credentials JSON (mode 0600), and
   returns an env dict with ``GOGDL_CONFIG_PATH``
   pointing at the tempdir + a cleanup callable;
2. Caller spawns the gogdl subprocess with this
   env;
3. Caller awaits the cleanup, which deletes the
   credentials file + tempdir.

The credentials JSON shape is what gogdl expects:
keyed by ``client_id``, with ``access_token``,
``refresh_token``, ``token_type``, ``expires_in``,
``scope``, ``created_at``, ``loginTime``. The
``expires_in=3600`` matches GOG's stated TTL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from ..config import GOGConfig
    CleanupFn = Callable[[], Awaitable[None]]

logger = logging.getLogger(__name__)


class _GogdlCreds:
    """Per-invocation gogdl credentials manager — internal.

    Holds nothing but the config; each
    ``acquire`` returns a fresh tempdir +
    cleanup pair so concurrent gogdl invocations
    don't share state.
    """

    def __init__(self, *, config: GOGConfig) -> None:
        """Stash the config (for client_id).

        Args:
            config: ``GOGConfig`` — only
                ``client_id`` is read.
        """
        self._config = config

    async def acquire(self, access_token: str,  refresh_token: str) -> tuple[dict[str, str], CleanupFn]:
        """Materialise a tempdir + creds file, return env dict + cleanup callable.

        Pipeline:

        1. ``mkdtemp`` for an isolated dir (off-
           thread since it can be slow on
           certain filesystems);
        2. Build the gogdl-shape dict;
        3. Write the JSON at mode 0600;
        4. Build the env dict (copy of current
           environ + ``GOGDL_CONFIG_PATH``);
        5. Return env + a cleanup closure that
           knows the paths.

        Args:
            access_token: bearer.
            refresh_token: refresh.

        Returns:
            ``(env, cleanup)`` — caller spawns
            with ``env``, then awaits
            ``cleanup()`` (best-effort, doesn't
            propagate errors).
        """
        tmpdir = await asyncio.to_thread(tempfile.mkdtemp, "unifideck-gogdl-")
        creds_path = os.path.join(tmpdir, "gog_credentials.json")
        gogdl_data = self._build_gogdl_data(access_token, refresh_token)

        await asyncio.to_thread(self._write_creds_sync, creds_path, gogdl_data)
        env = os.environ.copy()
        env["GOGDL_CONFIG_PATH"] = tmpdir
        cleanup = self._make_cleanup(creds_path, tmpdir)
        return env, cleanup

    def _build_gogdl_data(self, access_token: str, refresh_token: str) -> dict[str, dict[str, object]]:
        """Construct the per-client_id credentials dict gogdl expects.

        Both ``created_at`` and ``loginTime`` are
        set to ``time.time()`` — gogdl reads
        whichever it can find, depending on
        version.

        Args:
            access_token: bearer.
            refresh_token: refresh.

        Returns:
            ``{client_id: {access, refresh, ...}}``
            dict.
        """
        now = time.time()
        return {
            self._config.client_id: {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "openid",
                "created_at": now,
                "loginTime": now,
            },
        }

    @staticmethod
    def _write_creds_sync(creds_path: str, gogdl_data: dict[str, dict[str, object]]) -> None:
        """Write the credentials file with strict 0600 mode — blocking.

        Uses ``os.open`` with explicit mode so the
        file starts at 0600 from creation. JSON
        dumps without indentation (gogdl reads it,
        humans don't).

        Args:
            creds_path: destination.
            gogdl_data: payload.
        """
        fd = os.open(
            creds_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(gogdl_data, f)

    @staticmethod
    def _make_cleanup(creds_path: str, tmpdir: str) -> CleanupFn:
        """Build a cleanup closure that removes the creds file + tempdir.

        Closure captures the paths so the caller
        only needs to ``await cleanup()`` — no
        need to remember which files to remove.

        Errors during cleanup are logged at WARN
        and swallowed — there's nothing useful
        the caller can do at that point.

        Args:
            creds_path: file to remove.
            tmpdir: dir to rmdir.

        Returns:
            Async cleanup callable.
        """

        async def _cleanup() -> None:
            """Run the blocking cleanup in a worker thread.

            No return value, errors swallowed
            within ``_remove``.
            """

            def _remove() -> None:
                """Unlink the creds file + rmdir the tempdir; log + swallow OSErrors.

                Best-effort cleanup — if it fails
                we've already finished our work
                and the tempdir will be cleaned
                up on reboot anyway.
                """
                try:
                    if os.path.isfile(creds_path):
                        os.remove(creds_path)
                    if os.path.isdir(tmpdir):
                        os.rmdir(tmpdir)
                except OSError as e:
                    logger.warning(
                        "[GOGTokens] gogdl temp cleanup failed: %s",
                        e,
                    )

            await asyncio.to_thread(_remove)

        return _cleanup
