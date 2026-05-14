"""
Registry of installed Ubisoft games — tracks active installs across reboots.

OP-56f | py_modules/unifideck/stores/ubisoft/installer/registry.py

``UbisoftInstallerRegistry`` maintains a persistent JSON registry of
every Ubisoft game installed via Unifideck. Each entry records:

* the UPC ``space_id`` + Unifideck ``install_id`` (cross-ref via id_map);
* the install path inside the Wine prefix;
* the prefix path (for multi-prefix installs);
* the install timestamp + last-known launch timestamp.

The registry is read on store boot to rebuild the library state, and
written after every successful install/uninstall. It complements (does
not replace) the UPC-side game catalog: the registry is the authoritative
source for "which games does Unifideck know about", while UPC's catalog
is authoritative for "which games are licensed".
"""

from __future__ import annotations
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from ..config import UbisoftConfig

logger = logging.getLogger(__name__)
_INSTALLS_REG_SECTION_FMT = (
    "[Software\\\\WOW6432Node\\\\Ubisoft\\\\Launcher\\\\Installs\\\\{install_id}]"
)
_STEAM_COMPAT_ENV_VARS = (
    "SteamAppId",
    "SteamGameId",
    "STEAM_COMPAT_APP_ID",
)


def resolve_active_prefix_dir(
    prefix_path: str,
) -> str | None:
    """Pick the directory that holds the prefix's ``system.reg``.

    Tries ``<prefix>/pfx/`` first, then ``<prefix>/`` directly.

    Args:
        prefix_path: Prefix root.

    Returns:
        Absolute path to the registry-bearing directory, or
        ``None`` if neither layout exists.
    """
    prefix = Path(prefix_path)
    pfx = prefix / "pfx"
    if (pfx / "system.reg").is_file():
        return str(pfx)
    if (prefix / "system.reg").is_file():
        return str(prefix)
    return None


def read_system_reg(
    active_prefix: str,
) -> tuple[str, str] | None:
    """Read ``system.reg`` from a registry-bearing prefix.

    Args:
        active_prefix: Directory returned by
            ``resolve_active_prefix_dir``.

    Returns:
        Tuple ``(system_reg_path, content)``, or ``None`` on read failure.
    """
    system_reg = str(Path(active_prefix) / "system.reg")
    try:
        content = Path(system_reg).read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None
    return system_reg, content


def find_install_registry_section_bounds(
    content: str,
    section: str,
) -> tuple[int, int] | None:
    """Locate the byte-bounds of a registry section in a system.reg dump.

    Args:
        content: Full system.reg text.
        section: Section header line (e.g. ``[Software\\WOW6432Node\\...]``).

    Returns:
        Tuple ``(section_start, section_end)``, or ``None`` if
        the section isn't present.
    """
    if section not in content:
        return None
    sec_start = content.index(section)
    tail = content[sec_start + len(section) :]
    next_sec = re.search(r"\n\[", tail)
    sec_end = sec_start + len(section) + next_sec.start() if next_sec else len(content)
    return sec_start, sec_end


def _update_or_append_install_section(
    content: str,
    section: str,
    values: list[str],
) -> str:
    """Update existing keys in a registry section, or append new lines.

    Appends the section itself if missing; otherwise updates
    each ``"Key"="..."`` line in place (appending the key
    inside the section if not yet present).

    Args:
        content: Full system.reg text.
        section: Section header line.
        values: List of ``"Key"="Value"`` lines to write.

    Returns:
        Patched system.reg text.
    """
    bounds = find_install_registry_section_bounds(
        content,
        section,
    )
    if bounds is None:
        return content + f"\n{section}\n" + "\n".join(values) + "\n"
    sec_start, sec_end = bounds
    sec_body = content[sec_start + len(section) : sec_end]
    for val in values:
        key = val.split("=")[0]
        pattern = rf'^{re.escape(key)}="[^"]*"'
        new_body, count = re.subn(
            pattern,
            val,
            sec_body,
            flags=re.MULTILINE,
        )
        if count:
            sec_body = new_body
        else:
            sec_body = sec_body.rstrip("\n") + "\n" + val + "\n"
    return content[: sec_start + len(section)] + sec_body + content[sec_end:]


def inject_install_registry(
    prefix_path: str,
    install_id: str,
    install_dir: str,
) -> None:
    """Inject the UPC install-dir registry section into system.reg.

    Writes ``InstallDir`` under
    ``[Software\\WOW6432Node\\Ubisoft\\Launcher\\Installs\\<install_id>]``.
    Linux paths are converted to Wine paths (``Z:`` prefix +
    backslashes).

    Args:
        prefix_path: Wine prefix root.
        install_id: UPC install_id (per game).
        install_dir: Absolute Linux install path.
    """
    try:
        active_prefix = resolve_active_prefix_dir(prefix_path)
        if active_prefix is None:
            return
        loaded = read_system_reg(active_prefix)
        if loaded is None:
            return
        system_reg, content = loaded
        wine_path = install_dir
        if install_dir.startswith("/"):
            wine_path = "Z:" + install_dir.replace("/", "\\\\")
        section = _INSTALLS_REG_SECTION_FMT.format(
            install_id=install_id,
        )
        values = [f'"InstallDir"="{wine_path}"']
        content = _update_or_append_install_section(
            content,
            section,
            values,
        )
        Path(system_reg).write_text(
            content,
            encoding="utf-8",
        )
        logger.info(
            "[UbisoftInstaller] install registry injected for %s",
            install_id,
        )
    except Exception as e:
        logger.warning(
            "[UbisoftInstaller] registry injection failed: %s",
            e,
        )


def clean_install_registry(
    prefix_path: str,
    install_id: str,
) -> None:
    """Remove the install-dir registry section for one game.

    No-op when no install_id or no matching section is present.

    Args:
        prefix_path: Wine prefix root.
        install_id: UPC install_id.
    """
    if not install_id:
        return
    try:
        active_prefix = resolve_active_prefix_dir(prefix_path)
        if active_prefix is None:
            return
        loaded = read_system_reg(active_prefix)
        if loaded is None:
            return
        system_reg, content = loaded
        section = _INSTALLS_REG_SECTION_FMT.format(
            install_id=install_id,
        )
        bounds = find_install_registry_section_bounds(
            content,
            section,
        )
        if bounds is None:
            return
        sec_start, sec_end = bounds
        content = content[:sec_start] + content[sec_end:]
        Path(system_reg).write_text(
            content,
            encoding="utf-8",
        )
        logger.info(
            "[UbisoftInstaller] cleaned registry for %s",
            install_id,
        )
    except Exception as e:
        logger.warning(
            "[UbisoftInstaller] registry cleanup failed: %s",
            e,
        )


def get_directory_size(path: str) -> int:
    """Sum the file sizes under a directory tree.

    Per-file stat failures are silently skipped.

    Args:
        path: Directory root.

    Returns:
        Total size in bytes (0 on top-level walk failure).
    """
    total = 0
    try:
        for dirpath, _dirs, filenames in os.walk(path):
            for f in filenames:
                try:
                    total += (Path(dirpath) / f).stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def parse_positive_int(value: Any) -> int | None:
    """Parse a value as a positive int.

    Args:
        value: Any value to coerce.

    Returns:
        Positive int, or ``None`` on parse failure / non-positive.
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


class _ShortcutRegistry:
    """Read access to Unifideck's persistent shortcut-registry JSON.

    Used by the installer to find the Steam appid associated
    with the user's Ubisoft games — needed for compat-tool
    selection and registry injection.
    """

    def __init__(self, config: UbisoftConfig) -> None:
        """Bind the shortcut-registry helper to its config snapshot.

        Args:
            config: Frozen ``UbisoftConfig`` (provides the
                ``data_dir`` for registry I/O).
        """
        self._config = config

    def load(self) -> dict[str, Any]:
        """Load ``shortcuts_registry.json`` from the data dir.

        Returns:
            Parsed dict (empty on missing/malformed file).
        """
        path = Path(self._config.data_dir_expanded) / "shortcuts_registry.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(
                path.read_text(encoding="utf-8"),
            )
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "[UbisoftInstaller] shortcuts registry load failed: %s",
                e,
            )
            return {}
        if isinstance(data, dict):
            return data
        return {}

    def scan_for_ubisoft_appid(
        self,
        registry: dict[str, Any],
    ) -> int | None:
        """Find any positive Ubisoft appid in the registry.

        Args:
            registry: Parsed registry dict.

        Returns:
            First positive ``appid_unsigned`` under a
            ``ubisoft:*`` key, or ``None``.
        """
        for key, entry in registry.items():
            if not isinstance(key, str) or not key.startswith("ubisoft:"):
                continue
            if not isinstance(entry, dict):
                continue
            appid = parse_positive_int(
                entry.get("appid_unsigned"),
            )
            if appid:
                return appid
        return None

    def resolve_shortcut_appid(
        self,
        store_game_id: str | None,
    ) -> int | None:
        """Resolve the Steam appid associated with a Ubisoft launch.

        Resolution order: exact registry lookup by store_game_id →
        any Ubisoft entry → Steam compat env vars (SteamAppId,
        SteamGameId, STEAM_COMPAT_APP_ID) → any Ubisoft entry as a
        last resort.

        Args:
            store_game_id: Ubisoft store_game_id (may be ``None``).

        Returns:
            Steam appid (unsigned), or ``None`` if nothing resolves.
        """
        registry = self.load()
        if store_game_id:
            entry = registry.get(store_game_id, {})
            appid = parse_positive_int(
                entry.get("appid_unsigned"),
            )
            if appid:
                return appid
        appid = self.scan_for_ubisoft_appid(registry)
        if appid:
            return appid
        for env_var in _STEAM_COMPAT_ENV_VARS:
            appid = parse_positive_int(
                os.environ.get(env_var),
            )
            if appid:
                return appid
        if store_game_id:
            appid = self.scan_for_ubisoft_appid(registry)
            if appid:
                return appid
        return None
