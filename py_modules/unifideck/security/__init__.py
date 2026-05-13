"""Security primitives — token encryption + device identity + audit events.

OP-11 | py_modules/unifideck/security/__init__.py

Four cooperating components:

* ``device_identity`` — derives a stable per-device
  AES key from machine-id, hostname, and user uid;
* ``device_fingerprint`` — broader machine fingerprint
  state used to detect device-reset scenarios;
* ``secure_token_store`` — AES-GCM encryption of OAuth
  tokens / refresh-tokens at rest, with legacy plaintext
  migration support;
* ``audit_emitter`` (+ ``audit_decorators``) — fires
  ``SECURITY_*`` events on the bus for every privileged
  operation (token encrypt/decrypt, permission check,
  auth flow start/complete/fail).

Re-exports the public surface so consumers can
``from unifideck.security import SecureTokenStore,
emit_auth_started``.
"""

from .audit_emitter import (
    audit_auth_flow,
    emit_auth_completed,
    emit_auth_failed,
    emit_auth_started,
    emit_external_auth_check_failed,
    emit_legacy_plaintext_detected,
    emit_permissions_check,
    emit_token_file_migrated,
)
from .device_fingerprint import (
    DeviceFingerprint,
    FingerprintState,
)
from .device_identity import (
    DeviceIdentity,
    DeviceIdentityError,
    FakeDeviceIdentity,
)
from .secure_token_store import (
    SecureTokenStore,
    SecureTokenStoreError,
)

__all__ = [
    "DeviceIdentity",
    "DeviceIdentityError",
    "FakeDeviceIdentity",
    "DeviceFingerprint",
    "FingerprintState",
    "SecureTokenStore",
    "SecureTokenStoreError",
    "audit_auth_flow",
    "emit_auth_started",
    "emit_auth_completed",
    "emit_auth_failed",
    "emit_token_file_migrated",
    "emit_legacy_plaintext_detected",
    "emit_permissions_check",
    "emit_external_auth_check_failed",
]
