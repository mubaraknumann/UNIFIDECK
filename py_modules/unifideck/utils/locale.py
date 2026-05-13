"""Locale + market resolution — user preference, system, source fallback.

OP-21b | py_modules/unifideck/utils/locale.py

Resolves the active UI locale by walking three sources in
order:

1. User preference (config key ``ui.language``);
2. System locale (Python's ``locale.getlocale``), matched
   against the registered locales;
3. The source locale declared in ``i18n.locales``
   (typically ``en-US``).

The source ``i18n.locales`` schema is validated through
``scripts/locale_config.py`` (shared with the build-time
tools); ``_import_locale_config`` searches a couple of
candidate paths for it and falls back to "degraded mode"
when the script isn't reachable (e.g. running from a
checkout layout without the build scripts).

``get_unifideck_market`` is a convenience helper that
extracts the region part of the locale (``"US"`` from
``"en-US"``) — used by store APIs that take a market
code rather than a locale.
"""

from __future__ import annotations

import locale as _locale
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config_helpers import get_cfg

if TYPE_CHECKING:
    from ..config import ConfigManager

logger = logging.getLogger(__name__)

_USER_LANGUAGE_KEY = "ui.language"
_LOCALE_CONFIG_MODULE = None


def _import_locale_config():
    """Locate + import ``scripts/locale_config.py``, caching the result.

    Tries two candidate paths (one extra level up vs
    two), adds the first matching directory to
    ``sys.path``, and imports ``locale_config``. The
    module is cached in a module global so subsequent
    calls are O(1).

    Failure modes:

    * No candidate path matched → return ``None``
      (degraded mode);
    * Import itself fails → return ``None`` with a DEBUG
      log.

    Returns:
        The imported module, or ``None`` in degraded
        mode.
    """
    global _LOCALE_CONFIG_MODULE
    if _LOCALE_CONFIG_MODULE is not None:
        return _LOCALE_CONFIG_MODULE
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent.parent.parent / "scripts",
        here.parent.parent.parent / "scripts",
    ]
    for scripts_dir in candidates:
        target = scripts_dir / "locale_config.py"
        if target.is_file():
            scripts_str = str(scripts_dir)
            if scripts_str not in sys.path:
                sys.path.insert(0, scripts_str)
            try:
                import locale_config

                _LOCALE_CONFIG_MODULE = locale_config
                return _LOCALE_CONFIG_MODULE
            except ImportError as e:
                logger.debug(
                    "[locale] Found %s but import failed: %s",
                    target,
                    e,
                )
                return None
    logger.debug(
        "[locale] scripts/locale_config.py not found on any "
        "candidate path; locale resolution will use degraded "
        "mode",
    )
    return None


def get_locale_config(config: ConfigManager | None):
    """Load + validate the ``i18n.locales`` block from config.

    Three-step pipeline:

    1. Import the locale_config module (cached). Missing
       → return ``None`` (caller goes degraded).
    2. Read the ``i18n`` section. Missing or wrong-type
       → return ``None``.
    3. Run schema validation. Failures log at WARN +
       return ``None``.

    Args:
        config: optional ``ConfigManager``.

    Returns:
        Validated ``LocaleConfig`` instance, or ``None``
        if anything went wrong.
    """
    lc_module = _import_locale_config()
    if lc_module is None:
        return None
    i18n_section = get_cfg(config, "i18n", None)
    if not isinstance(i18n_section, dict):
        return None
    try:
        return lc_module.load_from_dict(
            {"i18n": i18n_section},
        )
    except Exception as e:
        logger.warning(
            "[locale] i18n schema validation failed at runtime: %s",
            e,
        )
        return None


def get_unifideck_locale(config: ConfigManager | None) -> str:
    """Resolve the active UI locale tag (e.g. ``"en-US"``).

    Three-source resolution chain:

    1. **User preference** (``ui.language``) — if it's
       a non-empty string. Underscore→hyphen
       normalisation (some legacy configs used
       ``en_US``). When a validated ``LocaleConfig`` is
       available the preference is checked against the
       registered locales; without validation the
       preference is accepted as-is (degraded mode).
    2. **System locale** — ``locale.getlocale()`` matched
       to the registered locales by language prefix.
    3. **Source locale** — declared by ``i18n.locales``
       (typically ``en-US``).

    Final fallback when nothing is available: hardcoded
    ``"en-US"`` with a WARN log.

    Args:
        config: optional ``ConfigManager``.

    Returns:
        Locale tag string.
    """
    lc = get_locale_config(config)
    saved = get_cfg(config, _USER_LANGUAGE_KEY, None)
    if isinstance(saved, str) and saved:
        normalised = saved.replace("_", "-")
        if lc is not None and lc.get(normalised) is not None:
            logger.debug(
                "[locale] user preference: %s",
                normalised,
            )
            return normalised
        if lc is None:
            logger.debug(
                "[locale] user preference (unvalidated): %s",
                normalised,
            )
            return normalised
        logger.debug(
            "[locale] user preference '%s' not in i18n.locales, falling back to system",
            saved,
        )
    system = _detect_system_locale(lc)
    if system:
        logger.debug("[locale] system: %s", system)
        return system
    if lc is not None:
        source_tag = str(lc.source.tag)
        logger.debug(
            "[locale] source fallback: %s",
            source_tag,
        )
        return source_tag
    logger.warning(
        "[locale] no config available, using hardcoded en-US",
    )
    return "en-US"


def get_unifideck_market(config: ConfigManager | None) -> str:
    """Extract the market / region part from the active locale tag.

    Examples:

    * ``"en-US"`` → ``"US"``
    * ``"fr-FR"`` → ``"FR"``
    * ``"ja"``    → ``"US"`` (fallback)

    Used by store APIs that want an ISO 3166 region
    code rather than a full locale tag.

    Args:
        config: optional ``ConfigManager``.

    Returns:
        Upper-case 2-letter region code, falling back to
        ``"US"``.
    """
    tag = get_unifideck_locale(config)
    parts = tag.split("-")
    if len(parts) >= 2 and len(parts[-1]) == 2:
        return parts[-1].upper()
    return "US"


def _detect_system_locale(lc: Any) -> str | None:
    """Match Python's reported system locale to a registered tag.

    Walks the registered locales looking for a prefix
    match on the language code (e.g. system ``fr_FR``
    matches registered ``fr-FR`` or ``fr`` alone).

    Returns ``None`` when:

    * ``lc`` is ``None`` (can't validate);
    * ``locale.getlocale()`` raises or returns nothing;
    * no registered locale matches the prefix.

    Args:
        lc: validated ``LocaleConfig`` or ``None``.

    Returns:
        Matched locale tag, or ``None``.
    """
    if lc is None:
        return None
    try:
        lang_tuple = _locale.getlocale()
    except (ValueError, TypeError) as e:
        logger.debug("[locale] getlocale() failed: %s", e)
        return None
    if not lang_tuple or not lang_tuple[0]:
        return None
    prefix = lang_tuple[0].split("_")[0].lower()
    if not prefix:
        return None
    for loc in lc.locales:
        if loc.tag.lower().startswith(prefix + "-") or loc.tag.lower() == prefix:
            return str(loc.tag)
    return None
