"""ConfigManager — merge defaults + user JSON with dotted-key access.

OP-10d | py_modules/unifideck/config/config_manager.py

The plugin's central config interface. Internally:

* Loads ``defaults/config.json`` (bundled) and the
  user override file (``~/.config/unifideck/config.json``);
* Deep-merges them over a hard-coded ``_FALLBACK``
  skeleton (so basic keys always exist even without a
  defaults file);
* Validates the ``i18n`` section against
  ``scripts/locale_config.py`` if reachable.

Public API:

* ``get(key, default)`` — dotted lookup with safe
  fallback;
* ``get_str`` / ``get_int`` / ``get_bool`` — typed
  accessors with coercion;
* ``set(key, value)`` — write to memory + persist to
  user file atomically;
* ``data_dir`` / ``cache_dir`` / ``path(*parts)`` —
  resolved filesystem paths;
* ``__getitem__`` / ``__contains__`` — pythonic dict
  interface.

``_FALLBACK`` is the minimum viable config — keys here
exist even when no file is found.
"""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_FALLBACK: dict[str, Any] = {
    "data_dir": "~/.local/share/unifideck",
    "ui": {
        "language": "en-US",
    },
    "sync": {
        "interval_seconds": 300,
        "cache_ttl_seconds": 3600,
    },
    "download": {
        "timeout_seconds": 30,
        "max_retries": 3,
        "artwork_concurrent": 4,
    },
    "stores": {
        "epic": {},
        "gog": {"client_secret": ""},
        "amazon": {},
        "ubisoft": {},
        "microsoft": {},
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` over ``base``, preferring override on conflicts.

    Per-key dispatch:

    * Both sides are dicts → recurse;
    * Otherwise → override wins (replaces ``base[k]``).

    Non-mutating: returns a new dict. Used by
    ``reload`` to layer defaults → user without
    aliasing.

    Args:
        base: lower-priority dict.
        override: higher-priority dict.

    Returns:
        Merged dict.
    """
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _get_by_path(d: dict[str, Any], dotted: str) -> Any:
    """Walk ``d`` along ``dotted``, raising ``KeyError`` on any missing segment.

    Plain ``a.b.c`` traversal. Strict: any non-dict
    intermediate or missing key raises the same
    ``KeyError(dotted)``, simplifying the caller's
    error handling.

    Args:
        d: root dict.
        dotted: ``"a.b.c"``-style key.

    Returns:
        Resolved value.

    Raises:
        KeyError: on any miss; carries the full dotted
            key for context.
    """
    node: Any = d
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(dotted)
        node = node[part]
    return node


def _set_by_path(d: dict[str, Any], dotted: str, value: Any) -> None:
    """Mutate ``d`` so that ``dotted`` resolves to ``value``, creating intermediates.

    Auto-creates missing intermediate dicts.
    Overwrites non-dict intermediates with fresh
    dicts (last write wins for the structure).

    Args:
        d: root dict (mutated).
        dotted: ``"a.b.c"``-style key.
        value: value to store at the leaf.
    """
    parts = dotted.split(".")
    node = d
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


class ConfigManager:
    """Three-layer config reader (fallback + defaults + user) with dotted access."""

    def __init__(
        self,
        defaults_path: str | None = None,
        user_path: str | None = None,
    ) -> None:
        """Store the paths and trigger an initial load.

        Both paths are optional — passing ``None`` for
        defaults means "fallback only" (rare; useful
        in tests). Passing ``None`` for user means
        "read-only config" (``set`` won't persist).

        Args:
            defaults_path: bundled defaults file path
                or ``None``.
            user_path: user override file path or
                ``None``.
        """
        self._defaults_path = Path(defaults_path) if defaults_path else None
        self._user_path = Path(user_path) if user_path else None
        self._merged: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        """Rebuild the merged config from fallback → defaults → user file.

        Tolerates read failures at both file layers
        (logs at WARN + skips). Stripping
        underscore-prefixed top-level keys from
        defaults is the in-file comment convention.

        After merge, runs ``_validate_i18n_schema``
        which may raise on a malformed i18n section
        (the only blocker — other config errors are
        non-fatal).
        """
        merged = dict(_FALLBACK)
        if self._defaults_path and self._defaults_path.exists():
            try:
                data = json.loads(
                    self._defaults_path.read_text(
                        encoding="utf-8",
                    ),
                )
                data = {k: v for k, v in data.items() if not k.startswith("_")}
                merged = _deep_merge(merged, data)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(
                    "[ConfigManager] defaults unreadable (%s): %s",
                    type(e).__name__,
                    e,
                )
        if self._user_path and self._user_path.exists():
            try:
                data = json.loads(
                    self._user_path.read_text(
                        encoding="utf-8",
                    ),
                )
                merged = _deep_merge(merged, data)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(
                    "[ConfigManager] user config unreadable (%s): %s",
                    type(e).__name__,
                    e,
                )
        self._merged = merged
        self._validate_i18n_schema()

    def _validate_i18n_schema(self) -> None:
        """Delegate i18n validation to ``scripts/locale_config.py`` if available.

        Locates the locale_config script relative to
        the defaults path (sibling ``scripts/``
        directory). Skips validation silently when
        the script isn't reachable (e.g. running from
        an ad-hoc checkout without the build
        scripts).

        Manipulates ``sys.path`` to import the script
        without altering the global path permanently
        — the inserted entry is removed in the
        ``finally`` block.

        Raises:
            LocaleConfigError: when the i18n section
                fails schema validation. This is the
                only fatal config error (everything
                else is warning + degraded mode).
        """
        if "i18n" not in self._merged:
            return
        if not self._defaults_path:
            return
        scripts_dir = self._defaults_path.parent.parent / "scripts"
        if not (scripts_dir / "locale_config.py").is_file():
            logger.debug(
                "[ConfigManager] locale_config.py not found at %s — "
                "skipping i18n schema validation",
                scripts_dir,
            )
            return
        import sys

        scripts_str = str(scripts_dir)
        added = False
        if scripts_str not in sys.path:
            sys.path.insert(0, scripts_str)
            added = True
        try:
            from locale_config import (
                LocaleConfigError,
                load_from_dict,
            )

            try:
                load_from_dict(self._merged)
            except LocaleConfigError as e:
                logger.error(
                    "[ConfigManager] i18n schema validation failed: %s",
                    e,
                )
                raise
        finally:
            if added:
                sys.path.remove(scripts_str)

    def get(self, key: str, default: Any = None) -> Any:
        """Read a dotted key with a fallback default.

        The workhorse for every config read in the
        plugin. Missing keys (any intermediate or the
        leaf) return ``default`` rather than raising.

        Args:
            key: dotted key (``"sync.interval_seconds"``).
            default: fallback value.

        Returns:
            Resolved value or ``default``.
        """
        try:
            return _get_by_path(self._merged, key)
        except KeyError:
            return default

    def get_str(self, key: str, default: str = "") -> str:
        """Read a key as a string, coercing via ``str()``.

        ``None`` falls back to ``default`` (not
        ``"None"``); other types get stringified.

        Args:
            key: dotted key.
            default: fallback string.

        Returns:
            String value.
        """
        v = self.get(key, default)
        return str(v) if v is not None else default

    def get_int(self, key: str, default: int = 0) -> int:
        """Read a key as an int, coercing via ``int()`` with safe fallback.

        Catches ``TypeError`` + ``ValueError`` →
        ``default``. Handles the common cases:

        * Numeric string (``"30"``) → int OK;
        * Float (``30.5``) → 30;
        * Non-numeric (``"abc"``) → default;
        * ``None`` → default.

        Args:
            key: dotted key.
            default: fallback int.

        Returns:
            Integer value.
        """
        v = self.get(key, default)
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        """Read a key as a bool, accepting common truthy strings.

        Three-arm coercion:

        * Already bool → as-is;
        * String → True iff lowercased value matches
          one of ``"true"``, ``"1"``, ``"yes"``,
          ``"on"``;
        * Other → ``bool(v)`` (Python's standard
          truthiness).

        Args:
            key: dotted key.
            default: fallback bool.

        Returns:
            Boolean value.
        """
        v = self.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in (
                "true",
                "1",
                "yes",
                "on",
            )
        return bool(v)

    def set(self, key: str, value: Any) -> None:
        """Write a key to memory + persist to the user file atomically.

        Two-step:

        1. Update the in-memory merged dict via
           ``_set_by_path`` (immediate effect for
           subsequent ``get`` calls);
        2. Re-read the user file, apply the same
           ``_set_by_path``, atomically write back
           with tmp + replace.

        Read-failure on the user file is treated as
        empty (start fresh). Write failure is logged
        at ERROR + tmp cleanup; the in-memory update
        is preserved (best effort: at least the live
        session has the new value).

        Args:
            key: dotted key.
            value: any JSON-serialisable value.
        """
        _set_by_path(self._merged, key, value)
        if not self._user_path:
            return
        user_data: dict[str, Any] = {}
        if self._user_path.exists():
            try:
                user_data = json.loads(
                    self._user_path.read_text(encoding="utf-8"),
                )
            except (json.JSONDecodeError, OSError):
                user_data = {}
        _set_by_path(user_data, key, value)
        self._user_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        tmp = self._user_path.with_suffix(
            self._user_path.suffix + ".tmp",
        )
        try:
            tmp.write_text(
                json.dumps(
                    user_data,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            tmp.replace(self._user_path)
        except OSError as e:
            logger.error(
                "[ConfigManager] failed to persist %s: %s",
                key,
                e,
            )
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    @property
    def data_dir(self) -> str:
        """Return the plugin data directory (expanded absolute path).

        Reads ``paths.data_dir`` first (preferred new
        key); falls back to ``data_dir`` at root
        (legacy key kept for backward compatibility).
        Always expands ``~``.

        Returns:
            Absolute path string.
        """
        raw = self.get("paths.data_dir") or self.get_str(
            "data_dir",
            "~/.local/share/unifideck",
        )
        return str(Path(raw).expanduser())

    @property
    def cache_dir(self) -> str:
        """Return the cache directory, defaulting to ``<data_dir>/cache``.

        Returns:
            Absolute path string.
        """
        raw = self.get("paths.cache_dir")
        if raw:
            return str(Path(raw).expanduser())
        return str(Path(self.data_dir) / "cache")

    def path(self, *parts: str) -> str:
        """Compose a subpath under ``data_dir`` (special-case ``"cache"``).

        The cache special-case mirrors the
        ``cache_dir`` property — callers expecting
        ``cm.path("cache")`` get the cache_dir
        resolution (which may diverge from
        ``data_dir/cache`` via ``paths.cache_dir``).

        Args:
            *parts: path segments under data_dir.

        Returns:
            Joined absolute path string.
        """
        if len(parts) == 1 and parts[0] == "cache":
            return self.cache_dir
        return str(Path(self.data_dir).joinpath(*parts))

    def __getitem__(self, key: str) -> Any:
        """Dict-style access — like ``get`` but raises on missing keys.

        Args:
            key: dotted key.

        Returns:
            Resolved value.

        Raises:
            KeyError: if not found (mirroring dict
                semantics).
        """
        val = self.get(key)
        if val is None:
            raise KeyError(key)
        return val

    def __contains__(self, key: str) -> bool:
        """Dict-style membership test — ``"a.b" in config``.

        Truthy when the key resolves to anything
        non-``None``. Note: a key explicitly set to
        ``None`` reports as missing — a fine
        approximation for the typical use.

        Args:
            key: dotted key.

        Returns:
            True if present and non-``None``.
        """
        return self.get(key) is not None
