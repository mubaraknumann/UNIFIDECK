"""On-disk encrypted GOG token storage with legacy plaintext migration.

OP-22-gog-tokens-storage | py_modules/unifideck/stores/gog/tokens/storage.py

The token file is a single JSON blob with
``access_token``, ``refresh_token``, ``username``,
``user_id`` — encrypted at rest via
``SecureTokenStore`` (Sprint 18 security pass).

Backwards-compat: a legacy plaintext JSON file is
*read* on first load (with an audit event
emitted), then re-saved encrypted on the next
write. This lets users upgrading from pre-18
plugins keep their session without re-authing.

Atomic writes (tempfile + ``os.replace`` with
mode 0600) protect against partial files on
crash. The companion gogdl-credentials mirror
file (legacy artifact, used to be written
alongside the token file) is now actively cleaned
up on every save — it's been superseded by the
``gogdl_credentials.write`` flow in the token
manager.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any, cast

from ....security import (
    SecureTokenStore,
    SecureTokenStoreError,
    emit_legacy_plaintext_detected,
    emit_permissions_check,
    emit_token_file_migrated,
)
from .user_info import GOGUserInfo

if TYPE_CHECKING:
    from ..config import GOGConfig

logger = logging.getLogger(__name__)


class _TokenStorage:
    """Encapsulates GOG token persistence — load, save, clear.

    Internal class (underscore prefix) — consumers
    use ``GOGTokenManager`` which owns one of
    these instances.

    Dependencies (config, bus, secure_store)
    injected via keyword-only constructor.
    """

    def __init__(
        self,
        *,
        config: GOGConfig,
        bus: Any,
        secure_store: SecureTokenStore,
    ) -> None:
        """Stash injected dependencies.

        Args:
            config: ``GOGConfig`` for the
                token file path + gogdl dir.
            bus: event bus for security audit
                events.
            secure_store: ``SecureTokenStore``
                for at-rest encryption.
        """
        self._config = config
        self._bus = bus
        self._secure_store = secure_store

    async def load(
        self,
    ) -> tuple[str, str, GOGUserInfo] | None:
        """Read tokens from disk, decrypting or migrating plaintext as needed.

        Pipeline:

        1. Check the file exists; missing → return
           ``None`` (no error logged — empty disk
           is the normal first-launch case);
        2. Read raw bytes in a worker thread;
        3. Parse via ``_parse_token_blob`` which
           handles encrypted + plaintext-legacy
           cases;
        4. Validate ``access_token`` +
           ``refresh_token`` both present + truthy;
        5. Build a ``GOGUserInfo`` from the
           ``username`` + ``user_id`` fields.

        Returns:
            ``(access_token, refresh_token,
            user_info)`` triple, or ``None``.
        """
        path = os.path.expanduser(self._config.token_file)
        if not os.path.isfile(path):
            return None

        def _read_sync() -> bytes | None:
            """Read the token file as raw bytes — blocking I/O.

            Returns:
                File contents, or ``None`` on
                read error.
            """
            try:
                with open(path, "rb") as f:
                    return f.read()
            except OSError as e:
                logger.warning("[GOGTokens] load failed: %s", e)
                return None

        blob = await asyncio.to_thread(_read_sync)
        if blob is None:
            return None
        data = self._parse_token_blob(blob, path)
        if not isinstance(data, dict):
            return None
        access = data.get("access_token")
        refresh = data.get("refresh_token")
        if not access or not refresh:
            return None
        user_info = GOGUserInfo(
            username=str(data.get("username", "")),
            galaxy_user_id=str(data.get("user_id", "")),
        )
        logger.info(
            "[GOGTokens] loaded tokens from disk (user=%s)",
            user_info.username or "unknown",
        )
        return access, refresh, user_info

    async def persist(
        self,
        access_token: str,
        refresh_token: str,
        user_info: GOGUserInfo,
    ) -> bool:
        """Encrypt + atomically write tokens to the configured token file.

        Pipeline:

        1. Encrypt the payload via
           ``SecureTokenStore``; encryption failure
           is fatal — we never fall back to
           plaintext (Sprint 18 policy);
        2. Atomic write via tempfile +
           ``os.replace`` (mode 0600);
        3. Clean up any stale gogdl-credentials
           mirror file from the legacy layout;
        4. Emit a ``permissions_check`` audit
           event so the security log shows the
           file mode is what we expect.

        Args:
            access_token: bearer.
            refresh_token: refresh.
            user_info: ``GOGUserInfo``.

        Returns:
            True on successful write.
        """
        path = os.path.expanduser(self._config.token_file)
        payload = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "username": user_info.username,
            "user_id": user_info.galaxy_user_id,
        }
        try:
            blob = self._secure_store.encrypt_payload(payload)
        except SecureTokenStoreError as e:
            logger.error(
                "[GOGTokens] cannot encrypt tokens: %s — "
                "refusing to write plaintext fallback",
                e,
            )
            return False
        ok = await asyncio.to_thread(
            self._write_token_file_atomic,
            path,
            blob,
        )
        if not ok:
            return False
        await self._remove_stale_gogdl_mirror()
        await self._emit_post_save_security(path)
        logger.info("[GOGTokens] saved tokens (encrypted)")
        return True

    async def clear_files(self) -> None:
        """Remove the token file + the legacy gogdl-credentials mirror.

        Best-effort: missing files are skipped,
        permission errors logged at WARN but don't
        propagate. Called from logout.
        """
        paths_to_remove = [
            os.path.expanduser(self._config.token_file),
            os.path.join(
                os.path.expanduser(
                    self._config.gogdl_config_dir,
                ),
                "gog_credentials.json",
            ),
        ]

        def _remove_sync() -> None:
            """Iterate the paths + unlink each; log + skip missing files.

            Runs in a worker thread (called via
            ``asyncio.to_thread``). Errors are
            logged at WARN but don't propagate.
            """
            for path in paths_to_remove:
                if not os.path.isfile(path):
                    continue
                try:
                    os.remove(path)
                    logger.info(
                        "[GOGTokens] removed %s",
                        path,
                    )
                except OSError as e:
                    logger.warning(
                        "[GOGTokens] could not remove %s: %s",
                        path,
                        e,
                    )

        await asyncio.to_thread(_remove_sync)

    @staticmethod
    def _write_token_file_atomic(path: str, blob: bytes) -> bool:
        """Tempfile + ``os.replace`` write with strict 0600 mode.

        Uses ``os.open`` with explicit flags +
        mode (rather than ``open(path, "wb")``)
        so the tempfile starts at 0600 from
        creation — no window where another user
        could read the in-progress file.

        Args:
            path: final destination.
            blob: encrypted bytes.

        Returns:
            True on success.
        """
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = path + ".tmp"
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            os.replace(tmp, path)
        except OSError as e:
            logger.warning(
                "[GOGTokens] save failed: %s",
                e,
            )
            return False
        return True

    async def _emit_post_save_security(self, path: str) -> None:
        """Stat the saved file and emit a ``permissions_check`` audit event.

        The security audit consumer cross-checks
        the mode against the expected 0600. We
        emit even if the mode is correct so the
        audit log shows the file *was* checked.

        Args:
            path: token file path.
        """

        def _stat_mode() -> int | None:
            """Read st_mode bits — blocking.

            Returns:
                12-bit mode (perms + sticky),
                or ``None`` on stat error.
            """
            try:
                st = os.stat(path)
                return st.st_mode & 0o7777
            except OSError:
                return None

        mode = await asyncio.to_thread(_stat_mode)
        if mode is not None:
            emit_permissions_check(
                self._bus,
                "gog",
                path,
                mode,
            )

    def _parse_token_blob(
        self,
        blob: bytes,
        path: str,
    ) -> dict[str, Any] | None:
        """Decode the token blob: encrypted format first, legacy plaintext fallback.

        Three paths:

        1. ``is_encrypted(blob)`` → decrypt via
           secure store; failure → log + return
           ``None``;
        2. Not encrypted → emit
           ``legacy_plaintext_detected`` audit
           event, parse as JSON;
        3. JSON parse error → log + return ``None``.

        The audit event in case 2 lets ops see
        which users still have legacy plaintext
        tokens (those files get re-encrypted on
        next save).

        Args:
            blob: raw file bytes.
            path: file path (for log/event
                context).

        Returns:
            Parsed dict, or ``None``.
        """
        if self._secure_store.is_encrypted(blob):
            try:
                return self._secure_store.decrypt_payload(blob)
            except SecureTokenStoreError as e:
                logger.warning(
                    "[GOGTokens] decrypt failed for %s: %s",
                    path,
                    e,
                )
                return None
        logger.info(
            "[GOGTokens] reading legacy plaintext token file "
            "at %s — will encrypt on next save",
            path,
        )
        emit_legacy_plaintext_detected(self._bus, "gog", path)
        try:
            return cast(
                "dict[str, Any] | None",
                json.loads(blob.decode("utf-8")),
            )
        except (ValueError, UnicodeDecodeError) as e:
            logger.warning(
                "[GOGTokens] legacy JSON parse failed: %s",
                e,
            )
            return None

    async def _remove_stale_gogdl_mirror(self) -> None:
        """Delete the legacy ``gog_credentials.json`` mirror; emit migration event.

        Old versions of the plugin wrote a second
        copy of the tokens to the gogdl config
        dir. The new flow writes a freshly-built
        gogdl-credentials file at install time
        instead. This call removes the legacy
        mirror so old + new files don't disagree.

        Emits ``token_file_migrated`` so the
        audit trail shows the cleanup.
        """
        stale = os.path.join(
            os.path.expanduser(self._config.gogdl_config_dir),
            "gog_credentials.json",
        )

        def _remove() -> bool:
            """Blocking unlink + log; returns whether anything was removed.

            Returns:
                True iff a stale file existed and
                was successfully removed.
            """
            if not os.path.isfile(stale):
                return False
            try:
                os.remove(stale)
                logger.info(
                    "[GOGTokens] removed stale gogdl mirror at %s",
                    stale,
                )
                return True
            except OSError as e:
                logger.warning(
                    "[GOGTokens] could not remove stale gogdl mirror %s: %s",
                    stale,
                    e,
                )
                return False

        removed = await asyncio.to_thread(_remove)
        if removed:
            emit_token_file_migrated(
                self._bus,
                "gog",
                stale,
                "",
            )


_ = time
