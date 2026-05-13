"""Edge profile directory manager — migration, cleanup, cookie ops.

OP-15c4 | py_modules/unifideck/auth/edge_browser/profile.py

The plugin uses an isolated Edge profile for the
auth flow so it doesn't interfere with the user's
regular Edge profile (if any). This module handles
the profile directory lifecycle:

* **Legacy migration** — if a previous plugin
  version left state at an old path, move it to
  the new one (preserves login state across plugin
  updates);
* **Stale singleton cleanup** — Edge crashes can
  leave behind ``Singleton{Lock,Cookie,Socket}``
  files pointing at dead processes; removing them
  is required before a new launch;
* **Cookie inspection** — query the Cookies SQLite
  DB to know whether the user is still logged in
  to Xbox (skip showing the login prompt if so);
* **Cookie clearing** — selectively delete Xbox /
  Microsoft cookies (logout) or wipe the whole
  profile (full reset).

The cookie DB is queried via a copied tempfile to
avoid contention with a running Edge process.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)


class EdgeProfileManager:
    """All-in-one manager for the isolated Edge auth profile.

    Five public operations: migrate, cleanup,
    inspect, clear-cookies, clear-everything. The
    constructor captures every path the manager
    needs so the per-operation methods stay
    parameter-free.
    """

    def __init__(
        self,
        *,
        profile_dir: str,
        log_file: str,
        legacy_profile_dir: str,
        legacy_log_file: str,
        cookie_domain_patterns: tuple[str, ...],
    ) -> None:
        """Capture the profile paths + cookie domain patterns.

        Keyword-only args because the constructor has
        five strings that look alike — positional
        confusion would be a real risk.

        Args:
            profile_dir: target profile directory.
            log_file: stderr log file.
            legacy_profile_dir: previous version's
                profile path (for migration).
            legacy_log_file: previous version's log
                path.
            cookie_domain_patterns: SQL LIKE patterns
                for cookie domains to clear on
                logout (e.g.
                ``("%xbox.com%", "%microsoft.com%")``).
        """
        self.profile_dir = profile_dir
        self.log_file = log_file
        self.legacy_profile_dir = legacy_profile_dir
        self.legacy_log_file = legacy_log_file
        self.cookie_domain_patterns = cookie_domain_patterns

    def migrate_legacy_profile(self) -> None:
        """Move the legacy profile to the new path if applicable.

        Three guards:

        * Legacy doesn't exist → no-op;
        * Both legacy AND new exist → skip (don't
          clobber a fresh profile with an old one);
        * Otherwise → ``shutil.move`` the directory
          (atomic on same filesystem).

        Also moves the legacy log file alongside,
        same conditions. Failures log at WARN and
        proceed — the user can re-auth if needed.
        """
        legacy_exists = Path(self.legacy_profile_dir).is_dir()
        new_exists = Path(self.profile_dir).is_dir()
        if not legacy_exists:
            return
        if new_exists:
            logger.debug(
                "[EdgeBrowser] both %s and %s exist; skipping migration",
                self.legacy_profile_dir,
                self.profile_dir,
            )
            return
        try:
            shutil.move(self.legacy_profile_dir, self.profile_dir)
            logger.info(
                "[EdgeBrowser] migrated legacy profile %s → %s",
                self.legacy_profile_dir,
                self.profile_dir,
            )
        except OSError as e:
            logger.warning(
                "[EdgeBrowser] legacy profile migration failed "
                "(%s → %s): %s — users may need to re-auth",
                self.legacy_profile_dir,
                self.profile_dir,
                e,
            )
        if Path(self.legacy_log_file).is_file() and not Path(self.log_file).is_file():
            try:
                shutil.move(self.legacy_log_file, self.log_file)
            except OSError:
                pass

    def _singleton_paths(self) -> list[str]:
        """Return the three Singleton* artifact paths in the profile.

        Chromium uses these three files to enforce
        one-process-per-profile. When the previous
        Edge died ungracefully they're left behind
        pointing at dead pids.

        Returns:
            Three-element list of full paths.
        """
        profile = Path(self.profile_dir)
        return [
            str(profile / "SingletonLock"),
            str(profile / "SingletonCookie"),
            str(profile / "SingletonSocket"),
        ]

    def _has_stale_singleton_socket(self) -> bool:
        """Detect a stale ``SingletonSocket`` symlink pointing at a dead target.

        Chromium creates ``SingletonSocket`` as a
        symlink whose target encodes the live pid.
        When the process dies the symlink remains
        but the target file doesn't — that's the
        stale state we detect here.

        Returns False on non-symlink (no
        ``SingletonSocket``, or it's a real file
        from a different Chromium version).

        Returns:
            True if cleanup is needed.
        """
        socket_path = Path(self.profile_dir) / "SingletonSocket"
        if not socket_path.is_symlink():
            return False
        try:
            target = socket_path.readlink()
        except OSError:
            return False
        return not Path(target).exists()

    def cleanup_stale_state(self) -> None:
        """Remove the three Singleton* files if a stale singleton is detected.

        Two-step:

        1. ``_has_stale_singleton_socket`` decides
           whether cleanup is needed (skip if not);
        2. Try unlinking each of the three paths;
           ``FileNotFoundError`` is expected (skip);
           other ``OSError`` logs at WARN.

        Successful removals are logged at INFO with
        the file names so operators see the
        cleanup happened.
        """
        if not self._has_stale_singleton_socket():
            return
        removed: list[str] = []
        for path in self._singleton_paths():
            try:
                Path(path).unlink()
                removed.append(Path(path).name)
            except FileNotFoundError:
                continue
            except OSError as e:
                logger.warning(
                    "[Edge] Failed to remove stale profile artifact %s: %s",
                    path,
                    e,
                )
        if removed:
            logger.info(
                "[Edge] Removed stale browser profile artifacts: %s",
                ", ".join(sorted(removed)),
            )

    def has_xbox_session(self) -> bool:
        """Inspect the Cookies SQLite DB for any ``%xbox.com%`` entry.

        Five-step:

        1. No Cookies file → return True (no
           previous session means we can attempt
           login without forcing a clear);
        2. Copy the DB to a tempfile (avoids
           contention with running Edge);
        3. SQLite COUNT(*) on the copy;
        4. Return True iff count > 0;
        5. Cleanup the tempfile in ``finally``.

        Any failure → return True (conservative; an
        unreadable DB is treated as "session
        might exist, don't force re-login").

        Returns:
            True if cookies suggest active session.
        """
        cookie_db = Path(self.profile_dir) / "Default" / "Cookies"
        if not cookie_db.exists():
            return True
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".db",
                delete=False,
            ) as tmp:
                tmp_path = tmp.name
            shutil.copy2(str(cookie_db), tmp_path)
            conn = sqlite3.connect(tmp_path, timeout=5)
            try:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%xbox.com%'",
                )
                count = cursor.fetchone()[0]
                return cast("bool", count > 0)
            finally:
                conn.close()
        except Exception as e:
            logger.debug("[Edge] Could not read cookie DB: %s", e)
            return True
        finally:
            if tmp_path and Path(tmp_path).exists():
                Path(tmp_path).unlink()

    def clear_cookies(self) -> None:
        """Delete Xbox / Microsoft cookies from the live DB (logout).

        Iterates ``cookie_domain_patterns`` and runs
        a ``DELETE FROM cookies WHERE host_key
        LIKE ?`` for each. Rollback on any exception
        inside the transaction.

        Edge must be stopped before this (we're
        writing the live DB, not a copy). Caller
        ensures that via ``graceful_kill``.

        Failures log at DEBUG only — clearing
        cookies is a "best effort logout" feature.
        """
        cookie_db = Path(self.profile_dir) / "Default" / "Cookies"
        if not cookie_db.exists():
            return
        try:
            conn = sqlite3.connect(str(cookie_db), timeout=5)
            try:
                for pattern in self.cookie_domain_patterns:
                    conn.execute(
                        "DELETE FROM cookies WHERE host_key LIKE ?",
                        (pattern,),
                    )
                conn.commit()
                logger.info(
                    "[Edge] Cleared Xbox/MS cookies from shared browser profile",
                )
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        except Exception as e:
            logger.debug(
                "[Edge] Could not clear shared browser cookies: %s",
                e,
            )

    def clear_profile_data(self) -> None:
        """Delete the entire profile directory + log file (full reset).

        Stronger than ``clear_cookies`` — drops
        ``localStorage``, ``IndexedDB``, cached
        media, everything. Used when the user
        explicitly requests "Forget account" or
        when corruption is detected.

        Skips symlinks (defensive against weird
        manual setups). Removal failures log at
        WARN. Successful removals are logged at
        INFO.
        """
        removed: list[str] = []
        for path in (self.profile_dir, self.log_file):
            path_obj = Path(path)
            if not path_obj.exists():
                continue
            try:
                if path_obj.is_dir() and not path_obj.is_symlink():
                    shutil.rmtree(path)
                else:
                    path_obj.unlink()
                removed.append(path_obj.name)
            except Exception as e:
                logger.warning(
                    "[Edge] Could not clear auth profile path %s: %s",
                    path,
                    e,
                )
        if removed:
            logger.info(
                "[Edge] Cleared auth state: %s",
                ", ".join(sorted(removed)),
            )
