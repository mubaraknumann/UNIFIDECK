"""Device fingerprint persistence — detect machine reset vs first-run.

OP-11c | py_modules/unifideck/security/device_fingerprint.py

The fingerprint file records a SHA-256 of the machine
id at first run, plus the first-seen and last-verified
timestamps. On every plugin boot:

* **No file** → ``is_new=True`` (first run; create
  the file).
* **Hash matches** → ``mismatch=False`` (normal
  case; update last_verified).
* **Hash differs** → ``mismatch=True`` (device id
  changed — probable disk migration or system
  rebuild; consumer triggers token wipe).

The frozen ``FingerprintState`` lets consumers safely
pass the result around without worrying about mutation.

File writes are atomic (tmp + replace) with 0o600
permissions so the fingerprint can't be tampered with by
other users.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass

from .device_identity import DeviceIdentity

logger = logging.getLogger(__name__)

_FORMAT_VERSION = 1


@dataclass(frozen=True)
class FingerprintState:
    """Result of ``verify_or_initialize`` / ``reinitialize``.

    Frozen so consumers can pass the state across the
    bus or stash it in caches without mutation
    concerns.

    Attributes:
        machine_id_hash: ``"sha256:<hex>"`` of the
            current device id.
        first_seen: unix timestamp the device was
            first registered (preserved across
            verifications).
        last_verified: unix timestamp of the current
            check.
        is_new: True on first-run initialization.
        mismatch: True when the stored hash differs
            from the current — probable device
            rebuild.
    """

    machine_id_hash: str
    first_seen: float
    last_verified: float
    is_new: bool = False
    mismatch: bool = False


class DeviceFingerprint:
    """Fingerprint file manager — verify, initialize, or detect mismatch."""

    def __init__(
        self,
        path: str,
        device_identity: DeviceIdentity | None = None,
    ) -> None:
        """Bind the fingerprint file path and the identity source.

        Path is expanded immediately (``~`` resolution
        is done once at construction). The identity
        source is injectable so tests can pass a fake;
        defaults to a fresh ``DeviceIdentity``.

        Args:
            path: fingerprint file path.
            device_identity: optional identity provider.
        """
        self._path = os.path.expanduser(path)
        self._device_identity = device_identity or DeviceIdentity()

    def verify_or_initialize(self) -> FingerprintState:
        """Read the file, compare to current id, return typed state.

        Three-arm dispatch:

        1. **No file** → ``_initialize`` (first run).
        2. **Hash mismatch** → return mismatch state
           without rewriting; the caller's job to
           decide whether to wipe + reinit or just
           warn.
        3. **Hash matches** → update last_verified +
           save + return normal state.

        Returns:
            ``FingerprintState`` describing the
            verification result.
        """
        current_hash = self._compute_current_hash()
        stored = self._load()
        if stored is None:
            return self._initialize(current_hash)
        if stored.get("machine_id_hash") != current_hash:
            return FingerprintState(
                machine_id_hash=current_hash,
                first_seen=float(stored.get("first_seen", 0.0)),
                last_verified=float(stored.get("last_verified", 0.0)),
                is_new=False,
                mismatch=True,
            )
        now = time.time()
        self._save(
            {
                "machine_id_hash": current_hash,
                "first_seen": float(stored.get("first_seen", now)),
                "last_verified": now,
                "version": _FORMAT_VERSION,
            }
        )
        return FingerprintState(
            machine_id_hash=current_hash,
            first_seen=float(stored.get("first_seen", now)),
            last_verified=now,
            is_new=False,
            mismatch=False,
        )

    def reinitialize(self) -> FingerprintState:
        """Force a fresh fingerprint file (drop any stored history).

        Used after a confirmed device reset where the
        caller has decided to clear the token store
        and start fresh. Equivalent to deleting the
        file + calling ``verify_or_initialize``.

        Returns:
            New ``FingerprintState`` with ``is_new=True``.
        """
        current_hash = self._compute_current_hash()
        return self._initialize(current_hash)

    def _compute_current_hash(self) -> str:
        """Read the device id and return ``"sha256:<hex>"`` of it.

        The prefix lets future versions migrate to
        different hash algorithms without ambiguity.

        Returns:
            Prefixed hex digest string.
        """
        mid = self._device_identity.read()
        digest = hashlib.sha256(mid.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    def _initialize(self, current_hash: str) -> FingerprintState:
        """Create the fingerprint file from scratch with ``first_seen=now``.

        Both timestamps are set to the same moment so
        the file is internally consistent on first
        run.

        Args:
            current_hash: precomputed hash to store.

        Returns:
            ``FingerprintState`` with ``is_new=True``.
        """
        now = time.time()
        payload = {
            "machine_id_hash": current_hash,
            "first_seen": now,
            "last_verified": now,
            "version": _FORMAT_VERSION,
        }
        self._save(payload)
        logger.info(
            "[DeviceFingerprint] initialized at %s",
            self._path,
        )
        return FingerprintState(
            machine_id_hash=current_hash,
            first_seen=now,
            last_verified=now,
            is_new=True,
            mismatch=False,
        )

    def _load(self) -> dict | None:
        """Read + parse the fingerprint file, tolerating every failure.

        Returns ``None`` on:

        * Missing file;
        * OSError on open / read;
        * Malformed JSON;
        * Non-dict payload (defensive).

        Failures other than missing-file log at WARN
        — file corruption is worth surfacing.

        Returns:
            Parsed dict or ``None``.
        """
        if not os.path.isfile(self._path):
            return None
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "[DeviceFingerprint] load failed: %s",
                e,
            )
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _save(self, payload: dict) -> None:
        """Atomically write ``payload`` to the fingerprint file with ``0o600`` mode.

        Uses ``os.open`` with the explicit mode arg so
        the temp file is created with restrictive
        permissions from the start (no race window
        where a wider-readable file exists).

        Best-effort: save errors log at WARN but don't
        raise. A failed save means the next boot
        will re-initialize, which is incorrect but
        not catastrophic.

        Args:
            payload: serialisable dict.
        """
        try:
            parent = os.path.dirname(self._path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = self._path + ".tmp"
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self._path)
        except OSError as e:
            logger.warning(
                "[DeviceFingerprint] save failed: %s",
                e,
            )
