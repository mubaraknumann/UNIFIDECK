"""
UPC session facade — propagate auth state across Wine prefixes.

OP-60a | py_modules/unifideck/stores/ubisoft/session/facade.py

UPC stores its auth state (credentials, refresh tokens, machine GUID)
inside the Wine prefix where the user signed in. To launch games from
other prefixes we have to copy that state into each prefix on demand.

``UbisoftSession`` is the orchestration class for this propagation. It
delegates to:

* ``reader.py`` (OP-60c) — read sessions out of the auth prefix;
* ``payload.py`` (OP-60b) — copy credentials/artifacts to target prefixes;
* ``propagator.py`` (OP-60d) — orchestrate propagation across multiple
  game prefixes when the auth state changes.

The session facade exposes the ``_read_machine_guid`` helper used by
the payload module's DPAPI-guard logic to refuse copying credentials
into a prefix with a different machine GUID (would corrupt the DPAPI
key vault).
"""

from __future__ import annotations
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from ..config import UbisoftConfig
from ..paths import UbisoftPrefixPaths
from .payload import _PayloadSync
from .propagator import _CredentialPropagator
from .reader import _CredentialReader

logger = logging.getLogger(__name__)
_CAPTURE_SENTINEL = "credentials_captured"


class UbisoftSession:
    """UPC session orchestration: read / propagate / capture credentials.

    Three responsibilities:
      * read state out of the auth prefix (``_CredentialReader``),
      * push it into other prefixes (``_PayloadSync`` + ``_CredentialPropagator``),
      * watch one prefix for new credentials and capture them
        when they appear (``capture``).

    The mtime sentinel file (``upc_session_file``) tracks the
    last captured credential mtime so subsequent captures only
    fire on real changes.
    """

    def __init__(
        self,
        config: UbisoftConfig,
        paths: UbisoftPrefixPaths,
        read_machine_guid: Callable[[str], str],
    ) -> None:
        """Build the credential reader, payload sync, and credential propagator.

        Args:
            config: Ubisoft store config.
            paths: Ubisoft prefix paths.
            read_machine_guid: Callable returning the
                ``MachineGuid`` registry value for a given prefix
                (used to scope encrypted credentials per prefix).
        """
        self._config = config
        self._paths = paths
        self._read_machine_guid = read_machine_guid
        self._reader = _CredentialReader(
            config=config,
            paths=paths,
        )
        self._payload = _PayloadSync(self)
        self._propagator = _CredentialPropagator(
            config=config,
            payload=self._payload,
            reader=self._reader,
        )

    def has_valid_credentials(self, prefix_path: str) -> bool:
        """Check whether the prefix has a usable ConnectSecureStorage vault.

        Args:
            prefix_path: Prefix to inspect.

        Returns:
            True iff any user home in the prefix has a CSS vault
            that exceeds the 100-byte sanity threshold.
        """
        return self._reader.has_valid_credentials(prefix_path)

    def get_credential_mtime(self, prefix_path: str) -> float:
        """Return the mtime of the prefix's CSS vault, if any.

        Args:
            prefix_path: Prefix to inspect.

        Returns:
            Unix mtime of the CSS file, or ``None`` if absent.
        """
        return self._reader.get_credential_mtime(prefix_path)

    def find_best_credential_source(self) -> str | None:
        """Locate the freshest CSS vault across every known prefix.

        Used as the source for retroactive credential propagation.

        Returns:
            Path string to the chosen CSS file, or ``None`` if no
            usable source exists.
        """
        return self._reader.find_best_credential_source()

    def _is_valid_css(self, css_path: str, min_size: int) -> bool:
        """Validate one CSS file against the configured minimum size.

        Args:
            css_path: Path to the CSS file.
            min_size: Minimum acceptable size (bytes).

        Returns:
            True iff the file exists and exceeds ``min_size``.
        """
        return self._reader._is_valid_css(css_path, min_size)

    def propagate_credentials_to_all(self) -> int:
        """Sync the DPAPI-encrypted credentials to every game prefix.

        Returns:
            Number of prefixes that actually received a fresh CSS.
        """
        return self._propagator.propagate_credentials_to_all()

    def propagate_auth_artifacts_to_all(self) -> int:
        """Sync ancillary auth artifacts (cookies, tokens) to every game prefix.

        Returns:
            Number of prefixes updated.
        """
        return self._propagator.propagate_auth_artifacts_to_all()

    def propagate_all_to_all(self) -> None:
        """Sync both credentials and auth artifacts to every game prefix.

        Equivalent to calling ``propagate_credentials_to_all`` then
        ``propagate_auth_artifacts_to_all``.
        """
        self._propagator.propagate_all_to_all()

    def inject_into_prefix(self, prefix_path: str) -> bool:
        """Inject credentials + auth artifacts into one specific prefix.

        Used at launch time so a freshly bootstrapped per-game
        prefix has current credentials before UPC starts.

        Args:
            prefix_path: Target prefix.

        Returns:
            True iff at least one file was actually copied.
        """
        return self._propagator.inject_into_prefix(prefix_path)

    def ensure_auth_state_in_prefixes(
        self,
        prefix_paths: list[str],
    ) -> int:
        """Make sure every known prefix has a usable auth state.

        Walks prefixes that lack credentials and pulls them from
        the best available source. Combines source discovery and
        propagation in one call.

        Returns:
            Number of prefixes that received credentials.
        """
        return self._propagator.ensure_auth_state_in_prefixes(
            prefix_paths,
        )

    def retroactive_sync(self) -> dict[str, Any]:
        """Retroactively propagate fresh credentials to every prefix.

        Called from the auth flow after a successful sign-in.

        Returns:
            Dict ``{updated, sources, …}`` summarising what was
            propagated.
        """
        return self._propagator.retroactive_sync()

    def capture(self, prefix_path: str) -> str | None:
        """Detect new credentials in one prefix and replicate to template + auth.

        Compares the credential mtime against the persisted sentinel;
        if newer, copies credentials and auth artifacts into the
        template and auth prefixes, then advances the sentinel.

        Args:
            prefix_path: Prefix to inspect.

        Returns:
            The capture sentinel string on success, ``None`` if no
            valid credentials or no change since last capture.
        """
        if not self._reader.has_valid_credentials(prefix_path):
            return None
        new_mtime = self._reader.get_credential_mtime(prefix_path)
        if not new_mtime:
            return None
        stored_mtime = self._read_stored_mtime()
        credentials_changed = new_mtime > stored_mtime
        if credentials_changed:
            self._write_stored_mtime(new_mtime)
            logger.info(
                "[UbisoftSession] detected new UPC "
                "credentials (ConnectSecureStorage.dat)",
            )
        for target in (
            self._config.template_dir_expanded,
            self._config.auth_prefix_dir_expanded,
        ):
            if not Path(target).is_dir():
                continue
            if Path(target).resolve() == Path(prefix_path).resolve():
                continue
            try:
                self._payload.sync_credentials_to_prefix(
                    prefix_path,
                    target,
                )
                self._payload.sync_auth_artifacts_to_prefix(
                    prefix_path,
                    target,
                )
            except Exception as e:
                logger.warning(
                    "[UbisoftSession] capture sync to %s failed: %s",
                    Path(target).name,
                    e,
                )
        if credentials_changed:
            logger.info(
                "[UbisoftSession] captured credentials → "
                "template + auth prefix updated",
            )
            return _CAPTURE_SENTINEL
        return None

    def _read_stored_mtime(self) -> float:
        """Read the persisted credential mtime sentinel from the session file.

        The file format is a single line ``credential_mtime:<float>``.
        Returns 0.0 on any read or parse error so the next capture
        is treated as fresh.

        Returns:
            The mtime as a float (Unix epoch), or 0.0.
        """
        session_file = self._config.upc_session_file_expanded
        if not Path(session_file).is_file():
            return 0.0
        try:
            content = (
                Path(session_file)
                .read_text(
                    encoding="utf-8",
                )
                .strip()
            )
        except OSError:
            return 0.0
        if not content.startswith("credential_mtime:"):
            return 0.0
        try:
            return float(content.split(":", 1)[1])
        except (ValueError, IndexError):
            return 0.0

    def _write_stored_mtime(self, mtime: float) -> None:
        """Persist the credential mtime sentinel for change detection.

        Creates the data dir if missing. Failures are logged but
        not raised — losing the sentinel only causes the next
        capture to over-fire once.

        Args:
            mtime: Unix mtime to record.
        """
        session_file = self._config.upc_session_file_expanded
        try:
            Path(self._config.data_dir_expanded).mkdir(
                parents=True,
                exist_ok=True,
            )
            Path(session_file).write_text(
                f"credential_mtime:{mtime}\n",
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(
                "[UbisoftSession] could not write mtime marker: %s",
                e,
            )

    def clear_session_file(self) -> None:
        """Remove the persisted session-mtime marker (best-effort).

        Called on logout to invalidate the next capture cycle.
        """
        session_file = self._config.upc_session_file_expanded
        if not Path(session_file).is_file():
            return
        try:
            Path(session_file).unlink()
            logger.info(
                "[UbisoftSession] removed UPC session marker",
            )
        except OSError as e:
            logger.warning(
                "[UbisoftSession] could not remove session marker: %s",
                e,
            )
