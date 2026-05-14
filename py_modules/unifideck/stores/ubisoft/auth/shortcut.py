"""
Steam shortcut creation for the auth flow — ensures a UPC launcher exists.

OP-58c | py_modules/unifideck/stores/ubisoft/auth/shortcut.py

``_AuthShortcut`` is responsible for creating (or re-using) a Steam
shortcut that launches UPC inside the auth-dedicated Wine prefix. The
shortcut is named "Ubisoft Connect", uses the UPC icon from SteamGridDB,
and is registered in Unifideck's shortcut registry with a stable
store_id (``ubisoft:upc-auth``) so it can be looked up later.

If a shortcut already exists in the registry, it's reused. If the
appid recorded in the registry doesn't match any actual Steam shortcut
(stale entry after the user reset Steam config), the entry is rebuilt
fresh.
"""

from __future__ import annotations
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from ....services.shortcut import ShortcutService
    from .facade import UbisoftAuth
logger = logging.getLogger(__name__)
_AUTH_LAUNCH_OPTIONS_TEMPLATE = (
    "{store_id} "
    "UNIFIDECK_UBISOFT_ACTION=auth "
    "UNIFIDECK_UBISOFT_PREFIX_NAME={prefix_name}"
)
_AUTH_SHORTCUT_NAME = "Ubisoft Connect"
_LEGACY_AUTH_LAUNCH_OPTIONS = "ubisoft:.template"
_ORPHAN_SHORTCUT_NAMES = frozenset(
    {"upc.exe", "ubisoft connect"},
)


def _prune_orphan_shortcuts(shortcuts: dict[str, Any]) -> int:
    """Remove orphaned ``upc.exe`` / ``ubisoft connect`` Steam shortcuts (in-place).

    Targets entries with no exe path and no launch options — 
    remnants of older Unifideck installs.

    Args:
        shortcuts: ``shortcuts`` sub-dict of the parsed VDF
            (mutated).

    Returns:
        Number of entries removed.
    """
    orphan_ids = [
        idx
        for idx, s in shortcuts.items()
        if s.get("AppName", "").lower() in _ORPHAN_SHORTCUT_NAMES
        and not s.get("exe", "").strip('"')
        and not s.get("LaunchOptions", "")
    ]
    for idx in orphan_ids:
        name = shortcuts[idx].get("AppName", "?")
        logger.info(
            "[UbisoftAuth] removing orphaned shortcut [%s] %r",
            idx,
            name,
        )
        del shortcuts[idx]
    return len(orphan_ids)


def _prune_legacy_template_shortcuts(
    shortcuts: dict[str, Any],
) -> int:
    """Remove entries left over from the ``ubisoft:.template`` flow (in-place).

    Args:
        shortcuts: ``shortcuts`` sub-dict of the parsed VDF
            (mutated).

    Returns:
        Number of entries removed.
    """
    legacy_ids = [
        idx
        for idx, s in shortcuts.items()
        if s.get("LaunchOptions", "") == _LEGACY_AUTH_LAUNCH_OPTIONS
    ]
    for idx in legacy_ids:
        logger.info(
            "[UbisoftAuth] removing legacy .template shortcut [%s]",
            idx,
        )
        del shortcuts[idx]
    return len(legacy_ids)


class _AuthShortcut:
    """Manage the Ubisoft Connect Steam shortcut used by the auth flow.

    Owns creation, validation, and re-creation of the
    non-Steam shortcut that launches UPC inside the dedicated
    auth Wine prefix. The shortcut's store_id
    (``ubisoft:upc-auth``) keys it in Unifideck's registry so
    later runs can find and reuse it across Steam restarts.
    """

    def __init__(self, parent: UbisoftAuth) -> None:
        """Bind the shortcut-helper to its parent auth orchestrator.

        Args:
            parent: Owning ``UbisoftAuth`` instance (provides
                config and plugin_dir for path resolution).
        """
        self._parent = parent

    def get_launcher_path(self) -> str:
        """Resolve the absolute path to the launcher dispatcher entry script.

        Falls back to a path derived from this file's location
        when the parent flow has no ``plugin_dir`` set.

        Returns:
            Absolute path to ``launcher/dispatcher.py``.
        """
        plugin_dir = self._parent._plugin_dir
        if not plugin_dir:
            plugin_dir = str(
                Path(__file__).resolve().parent.parent.parent.parent,
            )
        return str(
            Path(plugin_dir)
            / "py_modules"
            / "unifideck"
            / "launcher"
            / "dispatcher.py",
        )

    def build_auth_launch_options(self) -> str:
        """Build the Steam launch-options string for the auth shortcut.

        Encodes both the auth shortcut store_id and the
        dedicated prefix name (consumed by the dispatcher).

        Returns:
            Launch-options string ready to write into the VDF.
        """
        return _AUTH_LAUNCH_OPTIONS_TEMPLATE.format(
            store_id=(self._parent._config.auth_shortcut_store_id),
            prefix_name=self._parent._config.auth_prefix_name,
        )

    async def ensure_auth_shortcut(self) -> int | None:
        """Ensure a usable auth Steam shortcut exists, creating one if needed.

        Reuses the registry entry when present and valid, falls
        back to recreating the VDF entry if the registry knows
        an appid we can no longer find in Steam's VDF, and
        creates fresh entries (registry + VDF + artwork) as a
        last resort.

        Returns:
            Unsigned appid of the auth shortcut, or ``None`` if
            no shortcut service was wired (or the operation
            failed).
        """
        if self._parent._shortcut_service is None:
            logger.debug(
                "[UbisoftAuth] no shortcut_service; skipping auth shortcut creation",
            )
            return None
        try:
            sm = self._parent._shortcut_service
            store_id = self._parent._config.auth_shortcut_store_id
            existing_appid = await self.try_existing_shortcut(
                sm,
                store_id,
            )
            if existing_appid is not None:
                return existing_appid
            return await self.create_new_auth_shortcut(
                sm,
                store_id,
            )
        except Exception as e:
            logger.warning(
                "[UbisoftAuth] auth shortcut creation failed: %s",
                e,
            )
            return None

    async def try_existing_shortcut(
        self,
        sm: ShortcutService,
        store_id: str,
    ) -> int | None:
        """Reuse an existing auth shortcut from the registry if possible.

        Validates the VDF entry first. When the registry knows
        the appid but the VDF entry is missing, recreates the
        VDF entry and force-fetches artwork.

        Args:
            sm: ShortcutService.
            store_id: Registry key (``ubisoft:upc-auth``).

        Returns:
            Unsigned appid, or ``None`` if no registry entry
            exists or it can't be repaired.
        """
        registry = await self._parent._load_registry(sm)
        if store_id not in registry:
            return None
        vdf_found = await self.validate_auth_shortcut(sm)
        if vdf_found:
            uid = registry[store_id].get("appid_unsigned")
            if uid:
                await self._parent.fetch_auth_shortcut_artwork(
                    uid,
                )
                return cast("int | None", uid)
        entry = registry[store_id]
        appid = entry.get("appid")
        unsigned_id = entry.get("appid_unsigned")
        if not (appid and unsigned_id):
            return None
        logger.info(
            "[UbisoftAuth] recreating auth shortcut VDF from registry (appid=%d)",
            unsigned_id,
        )
        await self.add_shortcut_to_vdf(sm, appid)
        await self._parent._clear_compat(sm, appid)
        await self._parent.fetch_auth_shortcut_artwork(
            unsigned_id,
            force=True,
        )
        return cast("int | None", unsigned_id)

    async def create_new_auth_shortcut(
        self,
        sm: ShortcutService,
        store_id: str,
    ) -> int | None:
        """Create a fresh auth shortcut (VDF + registry + artwork).

        Generates a deterministic appid from the launcher path
        and shortcut name, prunes orphan / legacy entries from
        the VDF, inserts the new canonical entry, then registers
        and clears compat-tool state.

        Args:
            sm: ShortcutService.
            store_id: Registry key.

        Returns:
            Unsigned appid of the new shortcut.
        """
        launcher_path = self.get_launcher_path()
        appid = sm.generate_app_id(
            launcher_path,
            _AUTH_SHORTCUT_NAME,
        )
        unsigned_id = appid if appid >= 0 else appid + 2**32
        shortcuts_data = await sm.read_shortcuts()
        shortcuts = shortcuts_data.get("shortcuts", {})
        orphans_removed = _prune_orphan_shortcuts(shortcuts)
        legacy_removed = _prune_legacy_template_shortcuts(shortcuts)
        canonical_added = self._add_canonical_if_missing(
            shortcuts,
            launcher_path,
            appid,
            unsigned_id,
        )
        vdf_dirty = bool(
            orphans_removed or legacy_removed or canonical_added,
        )
        if vdf_dirty:
            await sm.write_shortcuts(shortcuts_data)
            logger.info(
                "[UbisoftAuth] VDF updated: orphans=%d legacy=%d added=%s",
                orphans_removed,
                legacy_removed,
                canonical_added,
            )
        await self._finalize_new_shortcut(sm, appid, unsigned_id)
        return cast("int | None", unsigned_id)

    async def _finalize_new_shortcut(
        self,
        sm: ShortcutService,
        appid: int,
        unsigned_id: int,
    ) -> None:
        """Finalize a newly-created auth shortcut: register, cleanup, artwork.

        Args:
            sm: ShortcutService.
            appid: Signed appid.
            unsigned_id: Unsigned appid for artwork.
        """
        await self._parent._register_shortcut(
            sm,
            appid,
            _AUTH_SHORTCUT_NAME,
        )
        await self._parent._cleanup_legacy_registry(sm)
        await self._parent._clear_compat(sm, appid)
        await self._parent.fetch_auth_shortcut_artwork(unsigned_id)

    def _add_canonical_if_missing(
        self,
        shortcuts: dict[str, Any],
        launcher_path: str,
        appid: int,
        unsigned_id: int,
    ) -> bool:
        """Append the canonical auth-shortcut entry to the VDF if not already present.

        Args:
            shortcuts: ``shortcuts`` sub-dict of the parsed VDF
                (mutated).
            launcher_path: Absolute path to the dispatcher.
            appid: Signed appid.
            unsigned_id: Unsigned appid (for logs).

        Returns:
            True iff the entry was added.
        """
        if self.shortcut_in_vdf(shortcuts):
            return False
        existing_indices = [int(k) for k in shortcuts if k.isdigit()]
        next_idx = max(existing_indices, default=-1) + 1
        shortcuts[str(next_idx)] = {
            "appid": appid,
            "AppName": _AUTH_SHORTCUT_NAME,
            "exe": f'"{launcher_path}"',
            "StartDir": f'"{Path(launcher_path).parent}"',
            "LaunchOptions": self.build_auth_launch_options(),
            "IsHidden": 1,
            "AllowDesktopConfig": 1,
            "OpenVR": 0,
            "tags": {"0": "Ubisoft"},
        }
        logger.info(
            "[UbisoftAuth] created auth shortcut in VDF (appid=%d)",
            unsigned_id,
        )
        return True

    async def validate_auth_shortcut(self, sm: ShortcutService) -> bool:
        """Verify the on-disk VDF matches our expected fields; repair drift.

        Looks for the entry by store_id (extracted from
        LaunchOptions). When found, repairs any drift in
        LaunchOptions / exe / StartDir / appid in place; if not
        found, logs a warning and returns False.

        Args:
            sm: ShortcutService.

        Returns:
            True iff the shortcut exists in the VDF (regardless
            of whether it was repaired). Returns True on any
            unexpected exception (graceful degradation).
        """
        try:
            launcher_path = self.get_launcher_path()
            expected_launch_options = self.build_auth_launch_options()
            expected_appid = sm.generate_app_id(
                launcher_path,
                _AUTH_SHORTCUT_NAME,
            )
            shortcuts_data = await sm.read_shortcuts()
            shortcuts = shortcuts_data.get("shortcuts", {})
            vdf_updated = False
            found = False
            for _idx, s in shortcuts.items():
                full_id = self.extract_store_id(
                    s.get("LaunchOptions", ""),
                )
                if full_id != (self._parent._config.auth_shortcut_store_id):
                    continue
                found = True
                if self._fix_shortcut_fields(
                    s,
                    launcher_path,
                    expected_launch_options,
                    expected_appid,
                ):
                    vdf_updated = True
                break
            if vdf_updated:
                await sm.write_shortcuts(shortcuts_data)
            if not found:
                logger.warning(
                    "[UbisoftAuth] auth shortcut not found in VDF during validation",
                )
                return False
            await self._parent._register_shortcut(
                sm,
                expected_appid,
                _AUTH_SHORTCUT_NAME,
            )
            await self._parent._clear_compat(
                sm,
                expected_appid,
            )
            return True
        except Exception as e:
            logger.warning(
                "[UbisoftAuth] auth shortcut validation failed: %s",
                e,
            )
            return True

    def _fix_shortcut_fields(
        self,
        entry: dict[str, Any],
        launcher_path: str,
        expected_launch_options: str,
        expected_appid: int,
    ) -> bool:
        """Repair drift in one VDF shortcut entry (in-place).

        Args:
            entry: One shortcut entry from the parsed VDF.
            launcher_path: Expected absolute launcher path.
            expected_launch_options: Expected LaunchOptions string.
            expected_appid: Expected signed appid.

        Returns:
            True iff any field was modified.
        """
        changed = False
        if entry.get("LaunchOptions", "") != expected_launch_options:
            logger.info(
                "[UbisoftAuth] auth shortcut launch options outdated, fixing",
            )
            entry["LaunchOptions"] = expected_launch_options
            changed = True
        current_exe = entry.get("exe", "").strip('"')
        if current_exe != launcher_path:
            logger.info(
                "[UbisoftAuth] auth shortcut exe outdated, fixing",
            )
            entry["exe"] = f'"{launcher_path}"'
            entry["StartDir"] = f'"{Path(launcher_path).parent}"'
            changed = True
        if entry.get("appid") != expected_appid:
            logger.info(
                "[UbisoftAuth] auth shortcut appid changed, fixing",
            )
            entry["appid"] = expected_appid
            changed = True
        return changed

    async def auth_shortcut_exists_in_vdf(self) -> bool:
        """Check whether the auth shortcut is present in Steam's VDF.

        Returns True (no-op) when no shortcut service is wired.

        Returns:
            True iff an entry with the matching store_id exists.
        """
        if self._parent._shortcut_service is None:
            return True
        try:
            sm = self._parent._shortcut_service
            shortcuts_data = await sm.read_shortcuts()
            shortcuts = shortcuts_data.get("shortcuts", {})
            target = self._parent._config.auth_shortcut_store_id
            return any(
                self.extract_store_id(
                    s.get("LaunchOptions", ""),
                )
                == target
                for s in shortcuts.values()
            )
        except Exception:
            return True

    async def add_shortcut_to_vdf(
        self,
        sm: ShortcutService,
        appid: int,
    ) -> None:
        """Insert the canonical auth-shortcut entry into the VDF and persist it.

        No-op when the entry already exists.

        Args:
            sm: ShortcutService.
            appid: Signed appid for the entry.
        """
        launcher_path = self.get_launcher_path()
        launch_options = self.build_auth_launch_options()
        shortcuts_data = await sm.read_shortcuts()
        shortcuts = shortcuts_data.get("shortcuts", {})
        if self.shortcut_in_vdf(shortcuts):
            return
        existing_indices = [int(k) for k in shortcuts if k.isdigit()]
        next_idx = max(existing_indices, default=-1) + 1
        shortcuts[str(next_idx)] = {
            "appid": appid,
            "AppName": _AUTH_SHORTCUT_NAME,
            "exe": f'"{launcher_path}"',
            "StartDir": f'"{Path(launcher_path).parent}"',
            "LaunchOptions": launch_options,
            "IsHidden": 1,
            "AllowDesktopConfig": 1,
            "OpenVR": 0,
            "tags": {"0": "Ubisoft"},
        }
        await sm.write_shortcuts(shortcuts_data)

    def shortcut_in_vdf(
        self,
        shortcuts: dict[str, Any],
    ) -> bool:
        """Return True iff the parsed VDF already contains the auth shortcut.

        Args:
            shortcuts: ``shortcuts`` sub-dict of the parsed VDF.

        Returns:
            True iff a matching store_id is present.
        """
        target = self._parent._config.auth_shortcut_store_id
        for s in shortcuts.values():
            full_id = self.extract_store_id(
                s.get("LaunchOptions", ""),
            )
            if full_id == target:
                return True
        return False

    @staticmethod
    def extract_store_id(launch_options: str) -> str:
        """Pull the leading store_id token out of a LaunchOptions string.

        The store_id is always the first whitespace-separated
        token (the dispatcher reads it positionally).

        Args:
            launch_options: VDF LaunchOptions value.

        Returns:
            The store_id token (empty string when no input).
        """
        if not launch_options:
            return ""
        return launch_options.split(maxsplit=1)[0]
