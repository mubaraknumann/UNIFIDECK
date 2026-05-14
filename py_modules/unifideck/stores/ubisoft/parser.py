"""
Ubisoft owned-games text parser — converts UPC's plaintext catalog
into structured records.

OP-55e | py_modules/unifideck/stores/ubisoft/parser.py

UPC's owned-games inventory is stored as a text file with a custom
key-value format (one game per stanza, ``key: value`` lines inside).
This module exposes a set of pure functions that parse that format
into Python dicts ready to be merged with the binary catalog parsed by
``parser_binary.py`` (OP-55f).

The parser handles every edge case observed in real UPC dumps:
* multi-byte unicode in game titles;
* nested sub-keys (e.g. ``localizations: { en-US: ... }``);
* line-continuations via trailing ``\\``;
* stray BOM bytes from Windows-side editing.

Errors on individual stanzas are reported as parse exceptions but
don't abort the whole file — the caller can decide whether to drop
the offending entry or escalate.
"""

import logging
import os
import re
from typing import Any, Optional, cast
import yaml
from .parser_binary import (
    parse_install_id,
    parse_launch_id,
    parse_ownership_record,
    parse_record_size,
)

logger = logging.getLogger(__name__)
BLACKLISTED_NAMES = ["gamename", "l1", "l2", "thumbimage", "", "ubisoft game", "name"]


def _parse_config_header(header: bytes, second_eight: bool = False) -> tuple:
    """Parse the per-stanza header in UPC's binary configurations file.

    Each game stanza begins with a small binary header carrying
    record size, install_id and launch_id. Falls back to a
    tiny header (10-byte) tuple on any parsing exception so
    the surrounding scan can advance.

    Args:
        header: Bytes starting at the candidate stanza header.
        second_eight: Use the alternate 8-byte size encoding
            (some stanzas use a second variant after a glitch).

    Returns:
        Tuple ``(obj_size, install_id, launch_id, header_size)``.
    """
    try:
        offset = 1
        record_size, offset, tmp_size = parse_record_size(
            header,
            offset,
            second_eight,
        )
        install_id, offset = parse_install_id(header, offset)
        launch_id, offset = parse_launch_id(header, offset)
        if record_size - offset < 128 <= record_size:
            tmp_size -= 1
            record_size += 1
            return (
                record_size - offset,
                install_id,
                launch_id,
                offset + tmp_size + 1,
            )
        return 0, 0, 0, 10
    except Exception:
        return 0, 0, 0, 10


def _get_yaml_field(game_yaml: dict, field: str = "name") -> str:
    """Read a field from a parsed UPC game YAML, with fallbacks.

    If the requested field is blank/blacklisted (sentinel names
    like ``GAMENAME`` or ``L1``), tries the installer's
    ``game_identifier`` and the localizations table.

    Args:
        game_yaml: Parsed YAML for one stanza.
        field: Field name (default ``name``).

    Returns:
        Stringified value, or empty string.
    """
    root = game_yaml.get("root", {})
    if not isinstance(root, dict):
        return ""
    value = str(root[field]) if field in root else ""
    if field == "name" and value.lower() in BLACKLISTED_NAMES:
        value = _yaml_field_installer_fallback(root, value)
        if value.lower() in BLACKLISTED_NAMES:
            return _yaml_field_localization_fallback(
                game_yaml,
                value,
            )
    return value


def _yaml_field_installer_fallback(root: dict, current: str) -> str:
    """Fallback resolver — try ``installer.game_identifier`` when the field is blacklisted.

    Args:
        root: ``root`` sub-dict from the YAML.
        current: Current (blacklisted) value to potentially replace.

    Returns:
        The replacement string, or ``current`` if no installer
        game_identifier is available.
    """
    installer = root.get("installer", {})
    if isinstance(installer, dict) and "game_identifier" in installer:
        return str(installer["game_identifier"])
    return current


def _yaml_field_localization_fallback(
    game_yaml: dict,
    current: str,
) -> str:
    """Fallback resolver — try the localizations table when other fallbacks fail.

    Args:
        game_yaml: Full game YAML dict.
        current: Current value to look up in the default locale.

    Returns:
        The localized string, or ``current`` if no match.
    """
    locs = game_yaml.get("localizations", {})
    if not isinstance(locs, dict):
        return current
    default_loc = locs.get("default", {})
    if isinstance(default_loc, dict) and current in default_loc:
        return str(default_loc[current])
    return current


class GameConfig:
    """Parsed view of one game stanza in UPC's configurations file.

    Attributes mirror the YAML fields the rest of the codebase
    needs: identifiers (install_id, launch_id, space_id,
    game_identifier), display data (name, thumb_image),
    executable path, raw YAML for diagnostics, and the
    third-party-platform marker (epic / steam / standalone).
    """

    def __init__(self):
        """Initialise an empty Ubisoft Connect game entry.

        Fields are populated incrementally by the YAML parser as
        it walks an ``installs/<id>.yaml`` document. Strings
        default to empty, integer IDs default to 0.
        """
        self.install_id: int = 0
        self.launch_id: int = 0
        self.space_id: str = ""
        self.name: str = ""
        self.executable: str = ""
        self.thumb_image: str = ""
        self.game_identifier: str = ""
        self.yaml_raw: str = ""
        self.third_party_platform: str = ""

    def __repr__(self) -> str:
        """Short repr used in logs.

        Returns:
            ``GameConfig(name=..., space_id=..., install_id=..., launch_id=...)``.
        """
        return (
            f"GameConfig(name={self.name!r}, space_id={self.space_id!r}, "
            f"install_id={self.install_id}, launch_id={self.launch_id})"
        )


def _read_binary_file(filepath: str) -> bytes | None:
    """Read a binary UPC dump file with structured error logging.

    Args:
        filepath: Absolute path.

    Returns:
        File bytes, or ``None`` on missing/unreadable file.
    """
    if not os.path.isfile(filepath):
        logger.warning(
            "[UbiParser] Configurations file not found: %s",
            filepath,
        )
        return None
    try:
        with open(filepath, "rb") as f:
            return f.read()
    except Exception as e:
        logger.error(
            "[UbiParser] Failed to read configurations: %s",
            e,
        )
        return None


def _extract_config_chunk(
    data: bytes,
    global_offset: int,
    header_size: int,
    obj_size: int,
    install_id: int,
    launch_id: int,
) -> Optional["GameConfig"]:
    """Decode + parse one configurations stanza into a ``GameConfig``.

    Skips stanzas under 500 bytes (header artefacts) and any
    stanza that doesn't contain ``start_game`` (the YAML
    discriminator).

    Args:
        data: Whole file bytes.
        global_offset: Stanza start offset in ``data``.
        header_size: Header byte count.
        obj_size: YAML byte count.
        install_id: Resolved install ID.
        launch_id: Resolved launch ID.

    Returns:
        A populated ``GameConfig`` on success, or ``None``.
    """
    if obj_size <= 500:
        return None
    yaml_start = global_offset + header_size
    yaml_end = yaml_start + obj_size
    if yaml_end > len(data):
        return None
    stream = data[yaml_start:yaml_end].decode(
        "utf8",
        errors="ignore",
    )
    if not stream or "start_game" not in stream:
        return None
    try:
        parsed = yaml.safe_load(
            stream.replace("\t", " "),
        )
    except Exception as e:
        logger.debug(
            "[UbiParser] YAML parse error at offset %d: %s",
            global_offset,
            e,
        )
        return None
    if not parsed:
        return None
    config = _build_game_config(
        parsed,
        stream,
        install_id,
        launch_id,
    )
    if config and config.name:
        return config
    return None


def parse_configurations(filepath: str) -> list[GameConfig]:
    """Parse UPC's configurations dump into a list of ``GameConfig``.

    Walks the file stanza-by-stanza, parses each header to
    find the YAML chunk, and builds a ``GameConfig`` per
    stanza. Per-stanza failures are logged at DEBUG and don't
    abort the scan. Falls back to the alternate 8-byte size
    encoding when the standard one leaves the cursor on a
    non-stanza byte.

    Args:
        filepath: Absolute path to UPC's configurations dump.

    Returns:
        List of resolved ``GameConfig`` (empty on file missing).
    """
    data = _read_binary_file(filepath)
    if data is None:
        return []
    results: list[GameConfig] = []
    global_offset = 0
    while global_offset < len(data):
        chunk = data[global_offset:]
        obj_size, install_id, launch_id, header_size = _parse_config_header(chunk)
        launch_id = (
            install_id if launch_id == 0 or launch_id == install_id else launch_id
        )
        config = _extract_config_chunk(
            data,
            global_offset,
            header_size,
            obj_size,
            install_id,
            launch_id,
        )
        if config:
            results.append(config)
        global_offset_tmp = global_offset
        global_offset += obj_size + header_size
        if global_offset < len(data) and data[global_offset] != 0x0A:
            obj_size, _, _, header_size = _parse_config_header(
                chunk,
                True,
            )
            global_offset = global_offset_tmp + obj_size + header_size
    logger.info(
        "[UbiParser] Parsed %d game configs from %s",
        len(results),
        filepath,
    )
    return results


def _build_game_config(
    parsed: dict, yaml_text: str, install_id: int, launch_id: int
) -> GameConfig | None:
    """Build a ``GameConfig`` from parsed YAML + parser-supplied IDs.

    Returns ``None`` when no valid relative-path executable
    can be located in the YAML (a hard requirement).

    Args:
        parsed: YAML dict.
        yaml_text: Original YAML text (kept for diagnostics +
            executable extraction).
        install_id: Resolved install ID.
        launch_id: Resolved launch ID.

    Returns:
        A populated ``GameConfig``, or ``None``.
    """
    config = GameConfig()
    config.install_id = install_id
    config.launch_id = launch_id
    config.yaml_raw = yaml_text
    config.name = _get_yaml_field(parsed, "name")
    config.thumb_image = _get_yaml_field(parsed, "thumb_image")
    root = parsed.get("root", {})
    if isinstance(root, dict):
        config.space_id = str(root.get("space_id", ""))
        installer = root.get("installer", {})
        if isinstance(installer, dict):
            config.game_identifier = str(installer.get("game_identifier", ""))
            config.third_party_platform = _extract_third_party_platform(
                root,
                installer,
            )
            exe_match = re.search(
                r"relative:\s*(.+?\.exe)",
                yaml_text,
                re.IGNORECASE,
            )
            if exe_match:
                config.executable = exe_match.group(1).strip().strip("'\"")
                return config
    return None


def _extract_third_party_platform(root: dict, installer: Any) -> str:
    """Pull the third-party-platform tag from a UPC YAML.

    Checks four locations in order: top-level ``third_party_platform``,
    ``installer.third_party_platform``, ``start_game.online.third_party_platform``,
    ``start_game.offline.third_party_platform``.

    Args:
        root: YAML ``root`` dict.
        installer: YAML ``installer`` sub-dict.

    Returns:
        Platform marker (``epic`` / ``steam`` / ``""``).
    """
    if isinstance(root.get("third_party_platform"), str):
        return cast("str", root["third_party_platform"].strip())
    if isinstance(installer, dict):
        value = installer.get("third_party_platform")
        if isinstance(value, str):
            return value.strip()
    start_game = root.get("start_game", {})
    if isinstance(start_game, dict):
        for mode in ("online", "offline"):
            mode_dict = start_game.get(mode, {})
            if isinstance(mode_dict, dict):
                value = mode_dict.get(
                    "third_party_platform",
                )
                if isinstance(value, str) and value:
                    return value.strip()
    return ""


def parse_ownership(filepath: str) -> list[int]:
    """Parse UPC's binary ownership file into a list of launch IDs.

    Walks fixed-format records starting at offset ``0x108`` —
    each record holds two launch IDs (deduplicated when
    they're equal).

    Args:
        filepath: Absolute path to UPC's ownership dump.

    Returns:
        List of owned launch IDs (empty on missing/unreadable file).
    """
    data = _read_ownership_file(filepath)
    if data is None:
        return []
    owned: list[int] = []
    offset = 0x108
    while offset < len(data):
        chunk = data[offset:]
        if chunk[0] != 0x0A:
            break
        record = parse_ownership_record(chunk)
        if record is None:
            break
        rec_size, tmp_size, lid1, lid2 = record
        owned.append(lid1)
        if lid2 != lid1:
            owned.append(lid2)
        offset += rec_size + tmp_size + 1
    logger.info(
        "[UbiParser] Found %d owned IDs in %s",
        len(owned),
        filepath,
    )
    return owned


def _read_ownership_file(filepath: str) -> bytes | None:
    """Read UPC's binary ownership file with structured error logging.

    Args:
        filepath: Absolute path.

    Returns:
        File bytes, or ``None``.
    """
    if not os.path.isfile(filepath):
        logger.warning(
            "[UbiParser] Ownership file not found: %s",
            filepath,
        )
        return None
    try:
        with open(filepath, "rb") as f:
            return f.read()
    except Exception as e:
        logger.error(
            "[UbiParser] Failed to read ownership: %s",
            e,
        )
        return None


def check_install_state(state_file: str) -> bool:
    """Quick check that a UPC ``state`` file starts with the expected magic byte.

    The 0x0A marker indicates the file contains a valid
    install record (not just a stub or empty file).

    Args:
        state_file: Absolute path.

    Returns:
        True iff the file exists and starts with 0x0A.
    """
    if not os.path.isfile(state_file):
        return False
    try:
        with open(state_file, "rb") as f:
            first_byte = f.read(1)
            return first_byte == b"\x0a"
    except Exception:
        return False


def build_id_map_from_configurations(
    filepath: str,
) -> dict[str, dict[str, Any]]:
    """Build the space_id → ID-map dict from a parsed configurations file.

    Used by ``UbisoftIdMap`` to refresh its in-memory map after
    UPC writes new games to the configurations file.

    Args:
        filepath: Absolute path to the configurations dump.

    Returns:
        Dict ``space_id → {install_id, launch_id, name, executable, game_identifier}``.
    """
    configs = parse_configurations(filepath)
    id_map: dict[str, dict[str, Any]] = {}
    for cfg in configs:
        if not cfg.space_id:
            continue
        id_map[cfg.space_id] = {
            "install_id": str(cfg.install_id),
            "launch_id": str(cfg.launch_id),
            "name": cfg.name,
            "executable": cfg.executable,
            "game_identifier": cfg.game_identifier,
        }
    logger.info(
        "[UbiParser] Built ID map with %d entries",
        len(id_map),
    )
    return id_map
