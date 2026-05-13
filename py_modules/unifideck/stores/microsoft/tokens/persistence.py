"""Encrypted at-rest persistence for Microsoft Live tokens.

OP-25-microsoft-tokens-persistence
File: py_modules/unifideck/stores/microsoft/tokens/persistence.py

Tokens are stored encrypted via
``SecureTokenStore``. The on-disk format
auto-detects between two layouts:

* New: encrypted blob (preferred);
* Legacy: plain JSON (read once, re-saved as
  encrypted on next save).

The legacy reader emits a
``legacy_plaintext_detected`` security event so
the migration can be observed in audit logs.

Writes are atomic (tempfile + rename) with mode
0600, so concurrent reads never see a partial
file and the file is never world-readable.

After a successful save, the mode is verified
and a ``permissions_check`` event is emitted —
catches misconfigured umask or NFS mounts that
might override the requested permissions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ....security import (
    SecureTokenStore,
    SecureTokenStoreError,
    emit_legacy_plaintext_detected,
    emit_permissions_check,
)

if TYPE_CHECKING:
    from ....event_bus.event_bus import EventBus

    from ..microsoft_config import MicrosoftConfig

logger = logging.getLogger(__name__)


class PersistenceMixin:
    """Load/save/clear methods backed by SecureTokenStore.

    Operates on attrs declared by the manager
    class (annotated here for type-checker
    visibility).
    """

    _ms_access_token: str | None
    _ms_refresh_token: str | None
    _token_saved_at: float
    _config: MicrosoftConfig
    _secure_store: SecureTokenStore
    _bus: EventBus | None

    async def load(self) -> bool:
        """Read + decrypt + populate in-memory state from the token file.

        Pipeline:

        1. Resolve path; missing → False;
        2. Read bytes via ``asyncio.to_thread``;
        3. ``_parse_blob`` returns dict or None
           (handles encrypted vs legacy
           transparently);
        4. Extract refresh_token; if absent,
           clear all state + return False
           (corrupt or partial file);
        5. Populate access + refresh +
           ``_token_saved_at`` from payload;
        6. Best-effort parse of ``saved_at`` as
           float (TypeError / ValueError →
           ``0.0`` so the next refresh-if-stale
           triggers immediately).

        Returns:
            True iff a usable refresh token
            was loaded.
        """
        path = str(Path(self._config.token_file).expanduser())
        if not Path(path).is_file():
            return False

        def _read_sync() -> bytes | None:
            """Sync read returning bytes or None on OSError.

            Returns:
                File content or ``None``.
            """
            try:
                return Path(path).read_bytes()
            except OSError as e:
                logger.warning(
                    "[MicrosoftTokens] load failed: %s",
                    e,
                )
                return None

        blob = await asyncio.to_thread(_read_sync)
        if blob is None:
            return False
        data = self._parse_blob(blob, path)
        if not isinstance(data, dict):
            return False
        refresh = data.get("refresh_token")
        if not refresh:
            self._ms_access_token = None
            self._ms_refresh_token = None
            self._token_saved_at = 0.0
            return False
        self._ms_access_token = data.get("access_token") or None
        self._ms_refresh_token = refresh
        try:
            self._token_saved_at = float(
                data.get("saved_at", 0.0),
            )
        except (TypeError, ValueError):
            self._token_saved_at = 0.0
        logger.info("[MicrosoftTokens] loaded tokens from disk")
        return True

    def _parse_blob(self, blob: bytes, path: str) -> dict[str, Any] | None:
        """Auto-detect encrypted vs legacy plaintext; parse and return dict.

        Three paths:

        * Encrypted blob → decrypt via
          SecureTokenStore. Failure logs WARN
          and returns None (caller treats as
          missing).
        * Legacy plaintext → log INFO, emit a
          ``legacy_plaintext_detected`` event,
          parse as UTF-8 JSON. Parse error logs
          WARN and returns None.
        * Either path may return None.

        Args:
            blob: raw bytes from disk.
            path: file path (used in log
                messages).

        Returns:
            Parsed dict or ``None``.
        """
        if self._secure_store.is_encrypted(blob):
            try:
                return self._secure_store.decrypt_payload(blob)
            except SecureTokenStoreError as e:
                logger.warning(
                    "[MicrosoftTokens] decrypt failed for %s: %s",
                    path,
                    e,
                )
                return None
        logger.info(
            "[MicrosoftTokens] reading legacy plaintext token "
            "file at %s — will encrypt on next save",
            path,
        )
        if self._bus is not None:
            emit_legacy_plaintext_detected(
                self._bus,
                "microsoft",
                path,
            )
        try:
            return cast(
                "dict[str, Any] | None",
                json.loads(blob.decode("utf-8")),
            )
        except (ValueError, UnicodeDecodeError) as e:
            logger.warning(
                "[MicrosoftTokens] legacy JSON parse failed: %s",
                e,
            )
            return None

    async def save(self) -> bool:
        """Encrypt + atomically write current tokens; verify permissions after.

        Skip-condition: when both tokens are
        None, just return True — no state to
        persist.

        ``encrypt_payload`` failure is fatal
        (return False); we refuse to fall back
        to plaintext writes because that would
        regress security.

        After a successful write, fire-and-
        forget the permission check.

        Returns:
            True on successful write.
        """
        if self._ms_access_token is None and self._ms_refresh_token is None:
            return True
        path = str(Path(self._config.token_file).expanduser())
        payload = {
            "access_token": self._ms_access_token,
            "refresh_token": self._ms_refresh_token,
            "saved_at": self._token_saved_at,
            "scope": self._config.scope,
        }
        try:
            blob = self._secure_store.encrypt_payload(payload)
        except SecureTokenStoreError as e:
            logger.error(
                "[MicrosoftTokens] cannot encrypt tokens: "
                "%s — refusing to write plaintext fallback",
                e,
            )
            return False
        ok = await asyncio.to_thread(
            _write_atomic_0600,
            path,
            blob,
        )
        await self._emit_permissions_after_save(ok, path)
        return ok

    async def _emit_permissions_after_save(self, ok: bool, path: str) -> None:
        """Stat the saved file and emit a permissions_check event with the mode.

        Skips when ``ok`` is False (failed save —
        nothing to check). Stat OSError →
        silently skip; nothing actionable.

        Args:
            ok: result of the save.
            path: file path.
        """
        if not ok:
            return

        def _stat() -> int | None:
            """Sync stat returning the file's mode bits or None.

            Returns:
                Mode (e.g. 0o600), or ``None``.
            """
            try:
                return Path(path).stat().st_mode & 0o7777
            except OSError:
                return None

        mode = await asyncio.to_thread(_stat)
        if mode is not None and self._bus is not None:
            emit_permissions_check(
                self._bus,
                "microsoft",
                path,
                mode,
            )

    async def clear(self) -> None:
        """Reset in-memory state and delete the token file from disk.

        Always clears memory. Disk delete is
        best-effort — OSError is logged at
        WARN, not raised, because the
        user-visible state (logged out) is
        achieved by the memory wipe alone.
        """
        self._ms_access_token = None
        self._ms_refresh_token = None
        self._token_saved_at = 0.0
        path = str(Path(self._config.token_file).expanduser())
        if Path(path).is_file():

            def _remove_sync() -> None:
                """Sync unlink with warning on failure.

                Inner closure capturing the
                resolved token-file path.
                OSError is logged at WARN
                and swallowed — clearing a
                non-existent or
                permission-denied file
                shouldn't fail the logout
                flow.

                Runs in a thread via
                ``asyncio.to_thread`` so
                the unlink doesn't block
                the event loop on slow
                disks.
                """
                try:
                    Path(path).unlink()
                except OSError as e:
                    logger.warning(
                        "[MicrosoftTokens] clear: could not remove %s: %s",
                        path,
                        e,
                    )

            await asyncio.to_thread(_remove_sync)


def _write_atomic_0600(path: str, blob: bytes) -> bool:
    """Write ``blob`` to ``path`` atomically with mode 0600.

    Five steps:

    1. mkdir -p the parent;
    2. ``os.open`` with O_CREAT and mode 0600
       (sets the perms at create time — safer
       than chmod after-the-fact);
    3. fdopen + write;
    4. ``os.replace`` (atomic rename) to final
       path;
    5. OSError → log WARN + return False.

    The ``.tmp`` suffix and same-directory
    placement ensure the rename is atomic on
    the same filesystem.

    Args:
        path: final file path.
        blob: bytes to write.

    Returns:
        True on success.
    """
    try:
        parent = str(Path(path).parent)
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
        except Exception:
            if fd >= 0:
                os.close(fd)
            raise
        os.replace(tmp, path)
        return True
    except OSError as e:
        logger.warning(
            "[MicrosoftTokens] save failed: %s",
            e,
        )
        return False
