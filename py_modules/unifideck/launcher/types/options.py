"""Launch option parsing — wrappers, env tokens, LSFG keys, and Proton flags from the raw argv string."""

from __future__ import annotations
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
_ENV_TOKEN_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")
_LSFG_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)$")
@dataclass
class ParsedOptions:
    """Result of parsing the Steam ``%command%`` style options string.

    Attributes:
        wrappers: Tokens that appear BEFORE ``%command%``
            (gamemoderun, mangohud, …).
        game_args: Tokens that appear AFTER ``%command%``.
        env_overrides: ``KEY=VALUE`` tokens lifted out of the
            argv into a dict (applied as environment).
        lsfg_requested: True iff an ``lsfg`` wrapper script
            or ``LSFG=1`` / ``ENABLE_LSFG=1`` env was set.
    """
    wrappers: list[str] = field(default_factory=list)
    game_args: list[str] = field(default_factory=list)
    env_overrides: dict[str, str] = field(default_factory=dict)
    lsfg_requested: bool = False
def parse_launch_options(raw: str) -> ParsedOptions:
    """Parse the raw options string into wrappers / game args / env / LSFG flag.

    Token rules:
      * ``KEY=VALUE`` (key matching ``[A-Z_][A-Z0-9_]*``) →
        ``env_overrides``
      * Tokens ending in ``/lsfg`` (after ``~`` expansion) →
        set ``lsfg_requested`` and are dropped
      * Tokens before ``%command%`` → ``wrappers``; after →
        ``game_args``. ``#%command%`` is dropped.
      * If no ``%command%`` was seen, all positional tokens
        are treated as game args.

    Args:
        raw: Raw options string (typically ``" ".join(argv[2:])``).

    Returns:
        A ``ParsedOptions``.
    """
    result = ParsedOptions()
    if not raw or not raw.strip():
        return result
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    remaining: list[str] = []
    for tok in tokens:
        m = _ENV_TOKEN_RE.match(tok)
        if m:
            result.env_overrides[m.group(1)] = m.group(2)
        else:
            remaining.append(tok)
    home = os.path.expanduser("~")
    lsfg_filtered: list[str] = []
    for tok in remaining:
        expanded = (
            tok.replace("~", home, 1)
            if tok.startswith("~") else tok
        )
        if expanded.endswith("/lsfg"):
            result.lsfg_requested = True
        else:
            lsfg_filtered.append(tok)
    if result.env_overrides.get("LSFG") == "1":
        result.lsfg_requested = True
    if result.env_overrides.get("ENABLE_LSFG") == "1":
        result.lsfg_requested = True
    _split_tokens_around_command(lsfg_filtered, result)
    return result

def _split_tokens_around_command(
    tokens: list[str], result: ParsedOptions,
) -> None:

    """Split positional tokens on ``%command%`` into wrappers vs game args.

    Args:
        tokens: Positional tokens (already filtered).
        result: Output ``ParsedOptions`` (mutated).
    """
    found_cmd = False
    for tok in tokens:
        if tok == "%command%":
            found_cmd = True
            continue
        if tok == "#%command%":
            continue
        if found_cmd:
            result.game_args.append(tok)
        else:
            result.wrappers.append(tok)
    if (
        not found_cmd
        and result.wrappers
        and not result.game_args
    ):
        result.game_args = result.wrappers
        result.wrappers = []
def apply_lsfg_env(
    opts: ParsedOptions,
    lsfg_script: Path | None = None,
) -> dict[str, str]:
    """Compute the LSFG env-overlay by parsing the user's ``~/lsfg`` script.

    Returns an empty dict when LSFG wasn't requested or the
    script is missing. Otherwise pulls every ``export KEY=VALUE``
    line out of the script (quotes stripped, KEY matching
    ``[A-Za-z_][A-Za-z0-9_]*``).

    Args:
        opts: Parsed options (``lsfg_requested`` is checked).
        lsfg_script: Override the script path (defaults to ``~/lsfg``).

    Returns:
        Dict of env-var overlays (always includes ``ENABLE_LSFG=1``
        when LSFG is active).
    """
    if not opts.lsfg_requested:
        return {}
    if lsfg_script is None:
        lsfg_script = Path(os.path.expanduser("~/lsfg"))
    if not lsfg_script.is_file():
        return {}
    overlay: dict[str, str] = {"ENABLE_LSFG": "1"}
    try:
        content = lsfg_script.read_text(
            encoding="utf-8", errors="replace",
        )
    except OSError:
        return overlay
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if (
            not line
            or line.startswith("#")
            or line.startswith("#!")
        ):
            continue
        if line.startswith("exec "):
            continue
        if not line.startswith("export "):
            continue
        kv = line[len("export "):]
        if "=" not in kv:
            continue
        key, _, value = kv.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and (
            (value[0] == '"' and value[-1] == '"')
            or (value[0] == "'" and value[-1] == "'")
        ):
            value = value[1:-1]
        if _LSFG_KEY_RE.match(key):
            overlay[key] = value
    return overlay