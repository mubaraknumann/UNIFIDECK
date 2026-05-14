"""
UPC payload sync between Wine prefixes.

OP-60b | py_modules/unifideck/stores/ubisoft/session/payload.py

``_PayloadSync`` copies credentials and auth-cache artifacts from one
Wine prefix to another. Two kinds of payload exist:

* **credentials** (``ConnectSecureStorage.dat``, ``user.dat``) —
  DPAPI-encrypted, bound to the machine GUID; sync requires the
  GUID match.
* **auth-cache artifacts** (settings, cookies, http2 cache, ownership
  cache) — not DPAPI-protected, sync without the guard.

The sync is idempotent: artifacts are hashed before copying so identical
files aren't re-copied. The hash function preserves a strict ordering
(files sorted alphabetically per directory, sub-dirs in filesystem
order) to keep digest stability across runs — caches built before this
ordering policy was applied may produce different hashes and trigger a
one-time re-sync.
"""

from __future__ import annotations
import hashlib
import logging
import os
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .facade import UbisoftSession
logger = logging.getLogger(__name__)
_CSS_MIN_SOURCE_SIZE = 10
_HASH_CHUNK_SIZE = 1024 * 1024


class _PayloadSync:
    """Copy UPC credentials and auth-cache artifacts between Wine prefixes.

    Handles two payload kinds: DPAPI-encrypted credentials (with
    MachineGuid guard) and unencrypted cache artifacts. Uses
    SHA-256 hashing for idempotent dedup so repeated syncs of
    identical files are no-ops.
    """

    def __init__(self, parent: UbisoftSession) -> None:
        """Bind the payload sync helper to its parent session.

        Args:
            parent: Owning ``UbisoftSession`` instance (provides
                config, paths, and the DPAPI guard).
        """
        self._parent = parent

    def sync_payload_to_prefix(
        self,
        source_prefix: str,
        target_prefix: str,
        *,
        payload_sources: dict[str, str],
        apply_dpapi_guard: bool,
        handle_directories: bool,
        log_label: str,
    ) -> int:
        """Generic payload sync — iterate per-user homes and copy each entry.

        Skips entirely when source/target resolve to the same path,
        when the payload is empty, or (with DPAPI guard) when the
        MachineGuids differ.

        Args:
            source_prefix: Source prefix root.
            target_prefix: Target prefix root.
            payload_sources: ``{rel_path: abs_src_path}``.
            apply_dpapi_guard: Skip on MachineGuid mismatch when True.
            handle_directories: True for cache artifacts (may be
                directory trees); False for credential files.
            log_label: Free-form label for logs.

        Returns:
            Number of entries actually copied.
        """
        if self.should_skip_payload_sync(
            source_prefix,
            target_prefix,
            payload_sources,
            apply_dpapi_guard,
        ):
            return 0
        synced = 0
        for _root, user_home in self._parent._paths.iter_user_homes(target_prefix):
            target_root = os.path.join(
                user_home,
                self._parent._config.upc_local_subdir,
            )
            for rel_path, src_path in payload_sources.items():
                dst_path = os.path.join(target_root, rel_path)
                if self.copy_payload_entry(
                    src_path,
                    dst_path,
                    handle_directories=handle_directories,
                    log_label=log_label,
                    rel_path=rel_path,
                ):
                    synced += 1
        return synced

    def should_skip_payload_sync(
        self,
        source_prefix: str,
        target_prefix: str,
        payload_sources: dict[str, str],
        apply_dpapi_guard: bool,
    ) -> bool:
        """Composite skip predicate for ``sync_payload_to_prefix``.

        Args:
            source_prefix: Source prefix root.
            target_prefix: Target prefix root.
            payload_sources: Empty dict triggers skip.
            apply_dpapi_guard: When True, MachineGuid mismatch triggers skip.

        Returns:
            True iff sync should be skipped.
        """
        if os.path.realpath(source_prefix) == os.path.realpath(target_prefix):
            return True
        if not payload_sources:
            return True
        if apply_dpapi_guard:
            source_guid = self._parent._read_machine_guid(
                source_prefix,
            )
            target_guid = self._parent._read_machine_guid(
                target_prefix,
            )
            if source_guid and target_guid and source_guid != target_guid:
                logger.warning(
                    "[UbisoftSession] MachineGuid mismatch: "
                    "source=%s… target=%s… — skipping "
                    "DPAPI sync",
                    source_guid[:8],
                    target_guid[:8],
                )
                return True
        return False

    def copy_payload_entry(
        self,
        src_path: str,
        dst_path: str,
        *,
        handle_directories: bool,
        log_label: str,
        rel_path: str,
    ) -> bool:
        """Copy one source path into the target, skipping when content is identical.

        Uses ``hash_artifact`` for the equality check, handles
        directory replacement when ``handle_directories=True``,
        and creates parent dirs as needed.

        Args:
            src_path: Source file or directory.
            dst_path: Destination path.
            handle_directories: Required when the source may be a tree.
            log_label: Free-form label for logs.
            rel_path: Relative path inside the prefix (for logs).

        Returns:
            True iff a copy actually happened (False if identical or on error).
        """
        if os.path.exists(dst_path):
            try:
                same = self.hash_artifact(src_path) == self.hash_artifact(dst_path)
            except OSError:
                same = False
            if same:
                return False
        try:
            parent = os.path.dirname(dst_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if handle_directories:
                if os.path.isdir(dst_path):
                    shutil.rmtree(
                        dst_path,
                        ignore_errors=True,
                    )
                elif os.path.exists(dst_path):
                    os.remove(dst_path)
                if os.path.isdir(src_path):
                    shutil.copytree(src_path, dst_path)
                else:
                    shutil.copy2(src_path, dst_path)
            else:
                shutil.copy2(src_path, dst_path)
            return True
        except OSError as e:
            logger.warning(
                "[UbisoftSession] %s copy failed for %s: %s",
                log_label,
                rel_path,
                e,
            )
            return False

    def sync_credentials_to_prefix(
        self,
        source_prefix: str,
        target_prefix: str,
    ) -> int:
        """Sync the DPAPI-encrypted credential files into a target prefix.

        Args:
            source_prefix: Source prefix (where credentials live).
            target_prefix: Destination prefix.

        Returns:
            Number of credential files actually copied.
        """
        return self.sync_payload_to_prefix(
            source_prefix=source_prefix,
            target_prefix=target_prefix,
            payload_sources=self.collect_credential_sources(
                source_prefix,
            ),
            apply_dpapi_guard=True,
            handle_directories=False,
            log_label="credential",
        )

    def collect_credential_sources(
        self,
        source_prefix: str,
    ) -> dict[str, str]:
        """Discover the credential files in a source prefix.

        Walks user homes (pfx_first=True). First file matching each
        of ``config.upc_credential_files`` wins.

        Args:
            source_prefix: Source prefix to scan.

        Returns:
            ``{credential_filename: absolute_path}``.
        """
        source_files: dict[str, str] = {}
        for _root, user_home in self._parent._paths.iter_user_homes(
            source_prefix,
            pfx_first=True,
        ):
            for fname in self._parent._config.upc_credential_files:
                if fname in source_files:
                    continue
                src = os.path.join(
                    user_home,
                    self._parent._config.upc_local_subdir,
                    fname,
                )
                if self._parent._is_valid_css(
                    src,
                    _CSS_MIN_SOURCE_SIZE,
                ):
                    source_files[fname] = src
        return source_files

    def sync_auth_artifacts_to_prefix(
        self,
        source_prefix: str,
        target_prefix: str,
    ) -> int:
        """Sync UPC's auth-cache artifacts (cookies, http2 cache, …).

        Args:
            source_prefix: Source prefix (where artifacts live).
            target_prefix: Destination prefix.

        Returns:
            Number of artifact files actually copied.
        """
        return self.sync_payload_to_prefix(
            source_prefix=source_prefix,
            target_prefix=target_prefix,
            payload_sources=self.collect_artifact_sources(
                source_prefix,
            ),
            apply_dpapi_guard=False,
            handle_directories=True,
            log_label="auth cache artifact",
        )

    def collect_artifact_sources(
        self,
        source_prefix: str,
    ) -> dict[str, str]:
        """Discover the cache-artifact paths in a source prefix.

        Walks user homes (pfx_first=True). First match for each
        of ``config.upc_auth_cache_artifacts`` wins.

        Args:
            source_prefix: Source prefix to scan.

        Returns:
            ``{rel_path: absolute_path}`` for present files and dirs.
        """
        artifacts: dict[str, str] = {}
        for _root, user_home in self._parent._paths.iter_user_homes(
            source_prefix,
            pfx_first=True,
        ):
            local_root = os.path.join(
                user_home,
                self._parent._config.upc_local_subdir,
            )
            for rel_path in self._parent._config.upc_auth_cache_artifacts:
                if rel_path in artifacts:
                    continue
                candidate = os.path.join(
                    local_root,
                    rel_path,
                )
                if os.path.isfile(candidate) or os.path.isdir(candidate):
                    artifacts[rel_path] = candidate
        return artifacts

    @staticmethod
    def hash_artifact(path: str) -> str:
        """Compute a SHA-256 fingerprint of a credentials artefact.

        Used to detect whether the captured auth payload changed
        between captures so the propagator can avoid redundant
        writes into every game prefix. Both files and directories
        are accepted; directories hash all contained file bytes
        in deterministic order.

        Args:
            path: File or directory to hash.

        Returns:
            Hex SHA-256 digest. For non-existent paths the empty
            digest of an unfed SHA-256 is returned.
        """
        digest = hashlib.sha256()
        if os.path.isdir(path):
            _PayloadSync._hash_directory_into(digest, path)
        elif os.path.isfile(path):
            _PayloadSync._hash_file_into(digest, path)
        return digest.hexdigest()

    @staticmethod
    def _hash_directory_into(digest: hashlib._Hash, path: str) -> None:
        """Update a hash digest with the contents of a directory tree.

        Walks files sorted alphabetically per directory (sub-dirs in
        filesystem order) for a stable hash. Each file's relative
        path is included in the digest so renames break equality.

        Args:
            digest: hashlib digest object (mutated).
            path: Directory root to walk.
        """
        for root, _dirs, files in os.walk(path):
            files.sort()
            for name in files:
                file_path = os.path.join(root, name)
                rel_path = os.path.relpath(file_path, path)
                digest.update(rel_path.encode("utf-8"))
                _PayloadSync._hash_file_into(digest, file_path)

    @staticmethod
    def _hash_file_into(digest: hashlib._Hash, path: str) -> None:
        """Update a hash digest with the contents of one file in 1 MB chunks.

        Read errors are silent (the file is omitted from the digest).

        Args:
            digest: hashlib digest object (mutated).
            path: File to read.
        """
        try:
            with open(path, "rb") as f:
                for chunk in iter(
                    lambda: f.read(_HASH_CHUNK_SIZE),
                    b"",
                ):
                    digest.update(chunk)
        except OSError:
            pass
