"""
Wine prefix path enumeration helpers.

OP-55c | py_modules/unifideck/stores/ubisoft/paths.py

``UbisoftPrefixPaths`` knows how to walk a Wine prefix and list the user
home directories inside it. Wine prefixes commonly contain multiple
"users" under ``drive_c/users/`` (e.g. ``steamuser``, ``Public``, plus
optionally per-Steam-user folders); the order in which they're visited
matters because UPC payload files are picked up from the *first* user
home that contains them.

Key method: ``iter_user_homes(prefix, pfx_first=False)`` which yields
``(root, user_home)`` tuples. The ``pfx_first`` flag is used by the
session-propagation code to ensure the prefix-default user is tried
before the Steam users — required for DPAPI credential matching.
"""

from __future__ import annotations
from collections.abc import Iterator
from pathlib import Path
from .config import UbisoftConfig


class UbisoftPrefixPaths:
    """Wine prefix path enumeration helpers for the Ubisoft sub-package.

    Knows where UPC binaries live, how to walk per-user homes inside
    a prefix, and how to build the per-game prefix path. The user-home
    iteration order matters because UPC payload files are picked from
    the first home that contains them.
    """

    def __init__(self, config: UbisoftConfig) -> None:
        """Bind the path helper to its config snapshot.

        Args:
            config: Frozen ``UbisoftConfig`` (provides the prefix
                roots and per-user subdir layout).
        """
        self._config = config

    def find_upc_exe(self, prefix_path: str) -> str | None:
        """Locate ``upc.exe`` inside the auth or template prefix.

        Args:
            prefix_path: Prefix to scan.

        Returns:
            Absolute (Linux) path string, or ``None`` if not found.
        """
        return self._find_in_prefix(
            prefix_path,
            self._config.upc_relative_path,
        )

    def find_connect_exe(self, prefix_path: str) -> str | None:
        """Locate ``UbisoftConnect.exe`` inside a prefix.

        Args:
            prefix_path: Prefix to scan.

        Returns:
            Absolute (Linux) path string, or ``None`` if not found.
        """
        return self._find_in_prefix(
            prefix_path,
            self._config.upc_connect_relative_path,
        )

    def find_configurations(
        self,
        prefix_path: str,
    ) -> str | None:
        """Locate the UPC ``configurations`` file inside a prefix.

        Args:
            prefix_path: Prefix to scan.

        Returns:
            Absolute path string, or ``None`` if not found.
        """
        return self._find_in_prefix(
            prefix_path,
            self._config.configurations_relative_path,
        )

    def iter_user_homes(
        self,
        prefix_path: str,
        pfx_first: bool = False,
    ) -> Iterator[tuple[str, str]]:
        """Yield ``(prefix_root, user_home)`` pairs across both prefix layouts.

        Iterates ``drive_c/users/<user>`` directories under both the
        prefix root and the ``pfx`` subdir. System users
        (``Public``, ``All Users``, ``Default``, ``Default User``) are
        skipped. ``pfx_first=True`` reverses the root order so the
        ``pfx`` user is tried first — required for DPAPI credential
        matching during session propagation.

        Args:
            prefix_path: Path to the Wine prefix root.
            pfx_first: If True, search the ``pfx`` users before the root.

        Yields:
            Tuples of (root_used, user_home_path).
        """
        roots = [
            prefix_path,
            str(Path(prefix_path) / "pfx"),
        ]
        if pfx_first:
            roots = list(reversed(roots))
        skip = set(self._config.wine_system_users)
        for prefix_root in roots:
            users_dir = Path(prefix_root) / "drive_c" / "users"
            if not users_dir.is_dir():
                continue
            try:
                entries = list(users_dir.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.name in skip:
                    continue
                if entry.is_dir():
                    yield prefix_root, str(entry)

    def get_prefix_path(self, space_id: str) -> str:
        """Return the per-game prefix path for one space_id.

        Honors the ``UNIFIDECK_UBISOFT_PREFIX_NAME`` env override
        (when set, all games share that prefix).

        Args:
            space_id: Ubisoft space_id.

        Returns:
            Absolute prefix path string.
        """
        return str(
            Path(self._config.prefixes_dir_expanded) / space_id,
        )

    @staticmethod
    def _find_in_prefix(
        prefix_path: str,
        relative: str,
    ) -> str | None:
        """Resolve a prefix-relative path under either layout (root or ``pfx``).

        Args:
            prefix_path: Path to the Wine prefix root.
            relative: Prefix-relative path (forward slashes).

        Returns:
            Absolute path string of the first match (file or dir),
            or ``None`` if neither layout contains it.
        """
        prefix = Path(prefix_path)
        for candidate in (
            prefix / relative,
            prefix / "pfx" / relative,
        ):
            if candidate.is_file() or candidate.is_dir():
                return str(candidate)
        return None
