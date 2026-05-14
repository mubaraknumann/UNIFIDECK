"""Atomic Windows registry write helpers used by the per-store language setup modules."""

from __future__ import annotations
import logging
import os
import re
import tempfile
from .matchers import smart_match_locale
from .resolver import _DEFAULT_LANGUAGE, LOCALE_MAP
logger = logging.getLogger(__name__)
def _resolve_prefix(prefix_path: str) -> str:
    """Resolve the directory holding the Wine registry files.

    Args:
        prefix_path: Any path inside (or equal to) the prefix.

    Returns:
        Path string to the registry-bearing directory.
    """
    from ..infrastructure.prefix_layout import resolve_registry_prefix
    return str(resolve_registry_prefix(prefix_path))
def _atomic_write_text(path: str, content: str) -> None:
    """Atomic text write (temp file + fsync + ``os.replace``).

    Args:
        path: Destination file path.
        content: Text to write.
    """
    target_dir = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=".reg.", suffix=".tmp", dir=target_dir,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

def _update_user_reg(
    prefix_path: str,
    lcid: str, slanguage: str, locale_name: str, scountry: str,
) -> bool:

    """Patch the ``Control Panel\\International`` section of user.reg.

    Writes the four locale keys (``Locale``, ``LocaleName``,
    ``sLanguage``, ``sCountry``). Existing values are updated
    in place; missing keys are appended. The whole section is
    created if absent.

    Args:
        prefix_path: Registry-bearing prefix directory.
        lcid: LCID hex code (e.g. ``"00000409"``).
        slanguage: 3-letter sLanguage code.
        locale_name: BCP-47 locale name.
        scountry: Display country name.

    Returns:
        True iff user.reg was patched (False if the file
        doesn't exist yet — prefix not initialised).
    """
    user_reg = os.path.join(prefix_path, "user.reg")
    if not os.path.exists(user_reg):
        logger.warning(
            "[language_setup] user.reg missing at %s — prefix not "
            "initialised yet", user_reg,
        )
        return False
    with open(user_reg, encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    section_header = "[Control Panel\\\\International]"
    new_values = {
        "Locale": lcid,
        "LocaleName": locale_name,
        "sLanguage": slanguage,
        "sCountry": scountry,
    }
    if section_header in content:
        section_start = content.index(section_header)
        body_start = section_start + len(section_header)
        next_section = re.search(r"\n\[", content[body_start:])
        section_end = (
            body_start + next_section.start()
            if next_section else len(content)
        )
        section_body = content[body_start:section_end]
        for key, value in new_values.items():
            pattern = rf'^"{re.escape(key)}"="[^"]*"'
            replacement = f'"{key}"="{value}"'
            new_body, count = re.subn(
                pattern, replacement, section_body, flags=re.MULTILINE,
            )
            if count > 0:
                section_body = new_body
            else:
                section_body = (
                    section_body.rstrip("\n") + f'\n"{key}"="{value}"\n'
                )
        content = (
            content[:body_start] + section_body + content[section_end:]
        )
    else:
        section = f"\n{section_header}\n"
        for key, value in new_values.items():
            section += f'"{key}"="{value}"\n'
        content += section
    _atomic_write_text(user_reg, content)
    logger.info(
        "[language_setup] wrote locale=%s to %s",
        locale_name, user_reg,
    )
    return True
def _apply_windows_locale(prefix_path: str, language: str) -> bool:
    """Apply a locale to user.reg, with fallback to en-US.

    Looks up the locale in ``LOCALE_MAP`` (with fuzzy matching
    via ``smart_match_locale``); falls back to ``en-US`` if the
    language has no mapping.

    Args:
        prefix_path: Any path inside the prefix.
        language: BCP-47 tag (e.g. ``"fr-FR"`` or ``"fr"``).

    Returns:
        True iff user.reg was patched.
    """
    resolved_prefix = _resolve_prefix(prefix_path)
    locale = smart_match_locale(language)
    if locale is None:
        logger.info(
            "[language_setup] no locale mapping for %s, using %s",
            language, _DEFAULT_LANGUAGE,
        )
        locale = LOCALE_MAP[_DEFAULT_LANGUAGE]
    return _update_user_reg(resolved_prefix, *locale)