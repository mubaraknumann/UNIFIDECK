"""Steam compat-tool override read/write + Proton settings persistence.

OP-12b | py_modules/unifideck/compatibility/proton_helpers.py

Steam stores per-app Proton overrides inside ``config.vdf``
under the ``CompatToolMapping`` section. This module reads
and writes that section without going through Steam's API
(which isn't available from a Decky plugin context).

Two state stores:

* ``config.vdf`` — Steam's binary VDF (text format)
  holding ``CompatToolMapping`` entries. Read with
  regex (full VDF parsing would be overkill — only
  one section matters).
* ``proton_settings.json`` — Unifideck's own preference
  file storing the user-chosen Proton tool per
  store_game_id (used to restore the override after
  certain launch flows).

A handful of module-level legacy free functions wrap
the singleton manager — older callers expected functional
APIs.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from ..config import ConfigManager

logger = logging.getLogger(__name__)

LINUX_RUNTIME_PREFIXES = (
    "steamlinuxruntime",
    "scout",
    "soldier",
    "sniper",
    "medic",
)
DEFAULT_CONFIG_VDF_RELATIVE = "config/config.vdf"
DEFAULT_PROTON_SETTINGS_RELATIVE = ".local/share/unifideck/proton_settings.json"
DEFAULT_SHORTCUTS_REGISTRY_RELATIVE = ".local/share/unifideck/shortcuts_registry.json"


@dataclass
class CompatToolResult:
    """Typed result for compat-tool read/write operations.

    Attributes:
        success: True on success.
        appid: target Steam AppID.
        tool_name: tool that was read or written.
        previous: prior value when ``set_for_app``
            overwrites; ``None`` otherwise.
        error: free-form error string on failure.
    """

    success: bool
    appid: int
    tool_name: str
    previous: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict.

        Returns:
            Five-key dict.
        """
        return {
            "success": self.success,
            "appid": self.appid,
            "tool_name": self.tool_name,
            "previous": self.previous,
            "error": self.error,
        }


def is_linux_runtime(tool_name: str) -> bool:
    """Return True if ``tool_name`` looks like a Linux runtime, not a Proton build.

    Two test patterns:

    * starts with a known runtime prefix
      (``steamlinuxruntime``, ``scout``, …);
    * contains ``_<prefix>`` as a substring (covers
      composite names like ``proton_scout``).

    Used by launch logic to decide whether to treat
    the configured "compat tool" as a real Proton
    override or as a Linux runtime that just sandboxes
    a native game.

    Args:
        tool_name: compat-tool name to classify.

    Returns:
        True if it's a Linux runtime.
    """
    if not tool_name:
        return False
    lower = tool_name.lower()
    return any(
        lower.startswith(prefix) or f"_{prefix}" in lower
        for prefix in LINUX_RUNTIME_PREFIXES
    )


def parse_compat_tool(content: str, appid: int) -> str:
    """Extract the compat-tool name for ``appid`` from ``config.vdf`` text.

    Regex-based parse (avoids pulling in a full VDF
    parser). Three short-circuits:

    * Empty content → ``""``;
    * AppID not mentioned anywhere → ``""``;
    * ``CompatToolMapping`` section not found → ``""``.

    Otherwise, looks for ``"appid" { ... "name" "..." }``
    within the section and extracts the name.

    Args:
        content: full ``config.vdf`` text.
        appid: target AppID.

    Returns:
        Tool name string, or ``""`` when nothing matched.
    """
    if not content:
        return ""
    appid_str = str(appid)
    if f'"{appid_str}"' not in content:
        return ""
    marker = '"CompatToolMapping"'
    marker_pos = content.find(marker)
    if marker_pos < 0:
        return ""
    pattern = re.compile(
        rf'"{appid_str}"\s*\{{([^}}]*)\}}',
        re.DOTALL,
    )
    m = pattern.search(content, marker_pos)
    if not m:
        return ""
    name_match = re.search(r'"name"\s+"([^"]*)"', m.group(1))
    return name_match.group(1) if name_match else ""


def inject_compat_tool(
    content: str,
    appid: int,
    tool_name: str,
) -> str:
    """Return ``content`` with the compat-tool entry for ``appid`` set to ``tool_name``.

    Two arms:

    1. **Replace existing** — if there's already an
       entry for ``appid``, update its ``name`` field
       in place.
    2. **Insert new** — otherwise, splice a fresh
       three-field block (``name`` / ``config`` /
       ``priority``) into the ``CompatToolMapping``
       section.

    Strict input validation: ``tool_name`` must match
    ``[A-Za-z0-9._-]*`` — anything else raises
    ``ValueError`` (prevents VDF injection attacks via
    malicious tool names).

    Args:
        content: original ``config.vdf`` text.
        appid: target AppID.
        tool_name: new tool name (empty string clears).

    Returns:
        Modified content string.

    Raises:
        ValueError: when ``tool_name`` has invalid
            characters.
    """
    if tool_name and not re.match(
        r"^[A-Za-z0-9._\-]*$",
        tool_name,
    ):
        raise ValueError(
            f"invalid compat tool name: {tool_name!r} (must match [A-Za-z0-9._-])",
        )
    if not content:
        return content
    appid_str = str(appid)
    pattern = re.compile(
        rf'("{appid_str}"\s*\{{[^}}]*"name"\s+)"[^"]*"',
        re.DOTALL,
    )
    new_content, count = pattern.subn(
        rf'\1"{tool_name}"',
        content,
        count=1,
    )
    if count > 0:
        return new_content
    marker = '"CompatToolMapping"'
    marker_pos = content.find(marker)
    if marker_pos < 0:
        return content
    open_brace = content.find("{", marker_pos)
    if open_brace < 0:
        return content
    insertion = (
        f'\n\t\t"{appid_str}"\n'
        f"\t\t{{\n"
        f'\t\t\t"name"\t\t"{tool_name}"\n'
        f'\t\t\t"config"\t\t""\n'
        f'\t\t\t"priority"\t\t"250"\n'
        f"\t\t}}"
    )
    return content[: open_brace + 1] + insertion + content[open_brace + 1 :]


class ProtonToolsManager:
    """Read/write Steam's compat-tool mappings + Unifideck's Proton prefs."""

    def __init__(self, config: ConfigManager | None = None) -> None:
        """Resolve every path the manager needs.

        Three paths held: the Steam ``config.vdf``,
        Unifideck's ``proton_settings.json``, and the
        shortcuts-registry JSON. All resolved at
        construction time so subsequent operations
        don't re-walk the filesystem.

        Args:
            config: optional ``ConfigManager`` for path
                overrides.
        """
        self._config = config
        self._config_vdf_path = self._resolve_config_vdf()
        self._proton_settings_path = self._resolve_proton_settings()
        self._shortcuts_registry_path = self._resolve_shortcuts_registry()

    def _resolve_config_vdf(self) -> Path:
        """Return the path to Steam's ``config.vdf``.

        Uses ``find_steam_path`` to locate the Steam
        root; falls back to ``~/.local/share/Steam`` if
        unavailable. Joins with the conventional
        ``config/config.vdf`` relative path.

        Returns:
            Resolved ``Path``.
        """
        from ..steam.library import find_steam_path

        steam = find_steam_path(self._config)
        if steam is None:
            return Path.home() / ".local/share/Steam" / DEFAULT_CONFIG_VDF_RELATIVE
        return Path(steam) / DEFAULT_CONFIG_VDF_RELATIVE

    def _resolve_proton_settings(self) -> Path:
        """Return the Unifideck Proton-settings JSON path (config-overridable).

        Returns:
            Expanded ``Path``.
        """
        return Path(
            self._cfg(
                "proton.settings_path",
                "~/" + DEFAULT_PROTON_SETTINGS_RELATIVE,
            ),
        ).expanduser()

    def _resolve_shortcuts_registry(self) -> Path:
        """Return the shortcuts-registry JSON path (config-overridable).

        Returns:
            Expanded ``Path``.
        """
        return Path(
            self._cfg(
                "proton.shortcuts_registry_path",
                "~/" + DEFAULT_SHORTCUTS_REGISTRY_RELATIVE,
            ),
        ).expanduser()

    def _cfg(self, key: str, default: Any) -> Any:
        """Safe config read — returns default on any failure.

        Args:
            key: dotted config key.
            default: fallback.

        Returns:
            Config value or default.
        """
        if self._config is None:
            return default
        try:
            return self._config.get(key, default)
        except Exception:
            return default

    def get_for_app(self, appid: int) -> CompatToolResult:
        """Read the configured compat tool for ``appid`` (empty string if none).

        Always returns ``success=True`` — there's no
        failure case from a read; absence of a mapping
        is signalled by ``tool_name=""``.

        Args:
            appid: Steam AppID.

        Returns:
            ``CompatToolResult``.
        """
        content = self._read_config_vdf()
        tool = parse_compat_tool(content, appid)
        return CompatToolResult(
            success=True,
            appid=appid,
            tool_name=tool,
        )

    def set_for_app(
        self,
        appid: int,
        tool_name: str,
    ) -> CompatToolResult:
        """Write a compat-tool override for ``appid``, returning the prior value.

        Four-step:

        1. Read current content (empty → fail
           ``config.vdf not readable``);
        2. Parse the previous value (for the return);
        3. Inject the new value;
        4. Write atomically (failure →
           ``config.vdf write failed``).

        Args:
            appid: Steam AppID.
            tool_name: new tool name (empty to clear).

        Returns:
            ``CompatToolResult`` with ``previous``
            populated.
        """
        content = self._read_config_vdf()
        if not content:
            return CompatToolResult(
                success=False,
                appid=appid,
                tool_name=tool_name,
                error="config.vdf not readable",
            )
        previous = parse_compat_tool(content, appid)
        new_content = inject_compat_tool(
            content,
            appid,
            tool_name,
        )
        if not self._write_config_vdf(new_content):
            return CompatToolResult(
                success=False,
                appid=appid,
                tool_name=tool_name,
                error="config.vdf write failed",
            )
        return CompatToolResult(
            success=True,
            appid=appid,
            tool_name=tool_name,
            previous=previous,
        )

    def clear_for_app(self, appid: int) -> CompatToolResult:
        """Clear the compat-tool override (equivalent to ``set_for_app(appid, "")``).

        Args:
            appid: Steam AppID.

        Returns:
            ``CompatToolResult``.
        """
        return self.set_for_app(appid, "")

    def list_known_tools(self) -> list[str]:
        """Enumerate compat tools installed on the system.

        Walks two well-known directories:

        * ``compatibilitytools.d`` — user-installed
          (GE-Proton etc.);
        * ``steamapps/common``    — Steam-managed
          (official Proton, Linux runtimes).

        Returns the names of every subdirectory found;
        callers can match these against
        ``CompatToolMapping`` values.

        Returns:
            Sorted list of tool directory names.
        """
        tools: list[str] = []
        steam_root = self._config_vdf_path.parent.parent
        for sub in ("compatibilitytools.d", "steamapps/common"):
            d = steam_root / sub
            if not d.is_dir():
                continue
            try:
                for child in sorted(d.iterdir()):
                    if child.is_dir():
                        tools.append(child.name)
            except OSError:
                continue
        return tools

    def _read_config_vdf(self) -> str:
        """Read ``config.vdf`` returning ``""`` on OSError (logged at ERROR).

        Uses ``errors="ignore"`` because Steam
        occasionally writes bytes that don't round-trip
        as UTF-8 (corrupted entries from older Steam
        versions); ignoring them lets us still read
        what we can.

        Returns:
            File contents, or ``""`` on read error.
        """
        try:
            return self._config_vdf_path.read_text(
                encoding="utf-8",
                errors="ignore",
            )
        except OSError as e:
            logger.error(
                "[proton_helpers] read %s failed: %s",
                self._config_vdf_path,
                e,
            )
            return ""

    def _write_config_vdf(self, content: str) -> bool:
        """Atomically write new ``config.vdf`` contents with fsync + rename.

        Five-step:

        1. Build a ``.vdf.tmp`` sibling;
        2. ``mkdir(parents=True)`` on the parent;
        3. Write + flush + fsync (durability);
        4. ``os.replace`` (atomic on POSIX);
        5. On error, attempt tmp cleanup.

        Failure logs at ERROR.

        Args:
            content: new file contents.

        Returns:
            True on success.
        """
        tmp = self._config_vdf_path.with_suffix(".vdf.tmp")
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._config_vdf_path)
            return True
        except OSError as e:
            logger.error(
                "[proton_helpers] write failed: %s",
                e,
            )
            try:
                tmp.unlink()
            except OSError:
                pass
            return False

    def load_proton_settings(self) -> dict[str, Any]:
        """Load Unifideck's Proton settings JSON, returning an empty skeleton on miss.

        Tolerates missing file and malformed JSON —
        returns ``{"games": {}}`` so callers always
        find the expected shape and can ``.setdefault``
        without checking.

        Returns:
            Parsed settings dict.
        """
        try:
            return cast(
                "dict[str, Any]",
                json.loads(self._proton_settings_path.read_text()),
            )
        except (OSError, json.JSONDecodeError):
            return {"games": {}}

    def save_proton_settings(
        self,
        data: dict[str, Any],
    ) -> bool:
        """Atomically persist Proton settings JSON.

        Same temp+rename pattern as
        ``_write_config_vdf`` but without ``fsync``
        (the settings file isn't critical enough to
        warrant the cost).

        Args:
            data: settings dict.

        Returns:
            True on success.
        """
        path = self._proton_settings_path
        tmp = path.with_suffix(".json.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, path)
            return True
        except OSError as e:
            logger.error(
                "[proton_helpers] save settings failed: %s",
                e,
            )
            return False


_singleton_pt_mgr = None


def _pt_mgr():
    """Return the module-level ``ProtonToolsManager`` singleton (lazy).

    Constructs on first call without a config (defaults
    only). Used by legacy free-function wrappers that
    don't propagate a config reference.

    Returns:
        Cached ``ProtonToolsManager``.
    """
    global _singleton_pt_mgr
    if _singleton_pt_mgr is None:
        _singleton_pt_mgr = ProtonToolsManager()
    return _singleton_pt_mgr


def get_compat_tool_for_app(appid_unsigned):
    """Legacy free-function — returns just the tool name string for an AppID.

    Args:
        appid_unsigned: Steam AppID (any numeric type).

    Returns:
        Tool name string (empty if none).
    """
    return _pt_mgr().get_for_app(int(appid_unsigned)).tool_name


def get_compat_tool_for_game(store_game_id):
    """Legacy stub — returns a placeholder result for a (store, game_id) pair.

    The original implementation looked up the
    Unifideck-mapped AppID; the stub returns a
    skeleton so existing callers don't crash but the
    actual lookup is now done elsewhere.

    Args:
        store_game_id: store-native game id.

    Returns:
        Stub dict.
    """
    return {
        "tool_name": "",
        "appid": 0,
        "store_game_id": store_game_id,
    }


def temporarily_clear_compat_tool(appid_unsigned):
    """Clear the override for an AppID, returning the prior value for later restore.

    Used by launch flows that need to ensure the game
    runs without an override (e.g. native Linux games
    that have a leftover Proton override). The prior
    value is returned so a later ``restore_compat_tool``
    call can put it back.

    Args:
        appid_unsigned: Steam AppID.

    Returns:
        ``{success, previous}`` dict.
    """
    result = _pt_mgr().clear_for_app(int(appid_unsigned))
    return {
        "success": result.success,
        "previous": result.previous,
    }


def restore_compat_tool(appid_unsigned, tool_name):
    """Restore a previously-cleared compat tool.

    Companion to ``temporarily_clear_compat_tool``.

    Args:
        appid_unsigned: Steam AppID.
        tool_name: tool to restore.

    Returns:
        ``{success}`` dict.
    """
    result = _pt_mgr().set_for_app(
        int(appid_unsigned),
        tool_name,
    )
    return {"success": result.success}


def save_proton_setting(store_game_id, tool_name):
    """Persist a per-game Proton preference into ``proton_settings.json``.

    Reads the settings file, sets
    ``games[store_game_id] = tool_name``, writes back.

    Args:
        store_game_id: store-native id (e.g.
            ``"epic:fortnite"``).
        tool_name: Proton tool name.

    Returns:
        ``{success}`` dict.
    """
    settings = _pt_mgr().load_proton_settings()
    settings.setdefault("games", {})[store_game_id] = tool_name
    return {
        "success": _pt_mgr().save_proton_settings(settings),
    }


def get_saved_proton_tool(store_game_id):
    """Read the saved Proton preference for a store_game_id (empty if none).

    Args:
        store_game_id: store-native id.

    Returns:
        Tool name string, or ``""`` if not saved.
    """
    return _pt_mgr().load_proton_settings().get("games", {}).get(store_game_id, "")


def resolve_proton_path(tool_name):
    """Legacy passthrough — returns the tool name unchanged.

    The original implementation mapped tool names to
    on-disk paths; the new launcher logic handles
    that itself, so this stub keeps the legacy API
    surface without doing real work.

    Args:
        tool_name: tool name.

    Returns:
        Same string.
    """
    return tool_name
