"""Smart matching between requested locale and gogdl-supported languages.

OP-22-gog-install-languages
File: py_modules/unifideck/stores/gog/install/languages.py

GOG's gogdl uses BCP-47-ish language codes
(``en``, ``de``, ``fr``, but also ``zh-Hans`` for
simplified Chinese). User locale might be
``en-US`` which won't match ``en`` directly — this
module handles the fallback.
"""

from __future__ import annotations


def smart_match_language(target: str, choices: list[str]) -> str | None:
    """Find the best gogdl language code matching the user's target locale.

    Three-stage match:

    1. Exact match (``target in choices``);
    2. Base-language match — split on ``"-"`` and
       compare the base codes case-insensitively
       (``en-US`` matches ``en``;
       ``zh-Hans`` matches ``zh``);
    3. No match → return ``None`` (caller falls
       back to gogdl's default).

    Empty inputs short-circuit to ``None``.

    Args:
        target: user's requested locale.
        choices: gogdl-supported language codes.

    Returns:
        Matched code from ``choices``, or ``None``.
    """
    if not target or not choices:
        return None
    if target in choices:
        return target
    target_base = target.split("-", maxsplit=1)[0].lower()
    for choice in choices:
        choice_base = choice.split("-")[0].lower()
        if target_base == choice_base:
            return choice
    return None
