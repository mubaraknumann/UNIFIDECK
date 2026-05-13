"""``unifideck://`` URI parser + verb registry.

OP-22a | py_modules/unifideck/actions/unifideck_uri.py

Defines the URI schema and parser for ``unifideck://`` deep
links. The same URI format is used by:

* frontend toast action buttons (e.g.
  ``unifideck://refresh-library/epic``);
* the backend RPC ``dispatch_unifideck_action`` entry point;
* external integrations (Steam shortcuts that need to
  trigger plugin actions).

The verb registry (`_VERB_REGISTRY`) is the single source of
truth for every supported verb. Each entry declares:

* scope — ``backend`` or ``frontend`` (who handles it);
* min/max arg count — validated by the parser;
* doc — human-readable description (used by docs / RPC
  introspection).

A successful parse returns a frozen ``ParsedAction`` so
downstream code can pass it around without worrying about
mutation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

SCOPE_BACKEND = "backend"
SCOPE_FRONTEND = "frontend"


@dataclass(frozen=True)
class ParsedAction:
    """Immutable result of ``parse_unifideck_uri``.

    Frozen so consumers can cache parsed actions safely.
    On parse failure, ``valid=False`` and ``error`` holds
    a machine-readable code (e.g. ``"empty_uri"``,
    ``"unknown_verb:foo"``) — the other fields may still
    be partially populated when the failure happened
    after some progress (e.g. wrong arg count knows the
    verb).

    Attributes:
        valid: True on a fully-validated URI.
        verb: parsed verb name (may be empty on early
            failures).
        scope: ``"backend"`` or ``"frontend"``.
        args: tuple of positional path segments.
        query: parsed query-string as flat string dict
            (only first value of each key).
        error: machine-readable failure code on
            ``valid=False``.
    """

    valid: bool
    verb: str = ""
    scope: str = ""
    args: tuple[str, ...] = ()
    query: dict[str, str] = field(default_factory=dict)
    error: str = ""


_VERB_REGISTRY: dict[str, tuple[str, int, int, str]] = {
    "auth": (
        SCOPE_BACKEND,
        1,
        1,
        "Start OAuth flow for a store. Args: <store>.",
    ),
    "retry-sync": (
        SCOPE_BACKEND,
        3,
        3,
        "Retry a cloud save sync. Args: <store> <game_id> <phase>. "
        "Phase is 'sync_down' or 'sync_up'.",
    ),
    "refresh-library": (
        SCOPE_BACKEND,
        1,
        1,
        "Refresh the game library for a single store. Args: "
        "<store>. Fire-and-forget: the RPC returns immediately "
        "and the sync runs in the background; the UI Library "
        "view picks up the result through its own SYNC_PROGRESS "
        "event subscription.",
    ),
    "refresh-all-libraries": (
        SCOPE_BACKEND,
        0,
        0,
        "Refresh every registered store's library. Args: none. "
        "Fire-and-forget same as refresh-library, but drives "
        "SyncService.sync_all() across all stores in parallel. "
        "Wired to the 'Refresh all libraries' button in the "
        "UnifideckSettingsPanel.",
    ),
    "open-save-folder": (
        SCOPE_FRONTEND,
        2,
        2,
        "Open the SaveFolderModal for a game. Args: <store> "
        "<game_id>. Frontend-only: the listener component "
        "opens the modal via Decky's showModal helper; the "
        "modal itself calls the backend list_save_folder RPC "
        "to populate its data.",
    ),
    "show-logs": (
        SCOPE_FRONTEND,
        1,
        1,
        "Open the LaunchLogsModal for a past launch. Args: "
        "<launch_id>. Frontend-only: the listener component "
        "opens the modal, which fetches logs via the backend "
        "get_launch_logs RPC. Used by launcher-failure toast "
        "actions on errorCircuitBreakerOpen and generic "
        "LAUNCHER_ERROR codes.",
    ),
    "settings": (
        SCOPE_FRONTEND,
        1,
        2,
        "Navigate to a settings section. Args: <section> "
        "[<focus_target>]. Frontend-only.",
    ),
}


def list_supported_verbs() -> list[str]:
    """Return every registered verb name, sorted alphabetically.

    Used by docs generation + the RPC introspection
    endpoint so the frontend can build the
    "supported verbs" debug panel without hard-coding
    the list.

    Returns:
        Sorted list of verb names.
    """
    return sorted(_VERB_REGISTRY.keys())


def parse_unifideck_uri(uri: str) -> ParsedAction:
    """Parse a ``unifideck://verb/arg1/arg2?key=val`` URI.

    Six-step validation pipeline:

    1. Empty input → ``empty_uri``.
    2. ``urlparse`` failure → ``parse_error:<exc>``
       (extremely rare; ``urlparse`` rarely raises but
       defensive ``except Exception`` keeps the contract
       safe).
    3. Wrong scheme (not ``unifideck``) →
       ``wrong_scheme:<actual>``.
    4. Missing verb (netloc) → ``missing_verb``.
    5. Unknown verb → ``unknown_verb:<verb>``.
    6. Wrong arg count → ``wrong_arg_count:got_N_expected_min_to_max``.

    Path segments are split on ``/`` and empty entries
    filtered (handles trailing slashes / double slashes).
    The query string is parsed as ``parse_qs`` then
    flattened to take only the first value of each key
    (verbs don't currently support multi-value queries).

    Args:
        uri: full URI string.

    Returns:
        ``ParsedAction`` — always returned, success state
        in ``.valid``.
    """
    if not uri:
        return ParsedAction(valid=False, error="empty_uri")
    try:
        parsed = urlparse(uri)
    except Exception as err:
        return ParsedAction(valid=False, error=f"parse_error:{err}")
    if parsed.scheme != "unifideck":
        return ParsedAction(
            valid=False,
            error=f"wrong_scheme:{parsed.scheme}",
        )
    verb = parsed.netloc
    if not verb:
        return ParsedAction(valid=False, error="missing_verb")
    if verb not in _VERB_REGISTRY:
        return ParsedAction(
            valid=False,
            verb=verb,
            error=f"unknown_verb:{verb}",
        )
    scope, min_args, max_args, _doc = _VERB_REGISTRY[verb]
    raw_path = parsed.path.lstrip("/")
    args = tuple(p for p in raw_path.split("/") if p) if raw_path else ()
    if not (min_args <= len(args) <= max_args):
        return ParsedAction(
            valid=False,
            verb=verb,
            scope=scope,
            error=(
                f"wrong_arg_count:got_{len(args)}_expected_{min_args}_to_{max_args}"
            ),
        )
    raw_query = parse_qs(parsed.query) if parsed.query else {}
    query = {k: v[0] for k, v in raw_query.items() if v}
    return ParsedAction(
        valid=True,
        verb=verb,
        scope=scope,
        args=args,
        query=query,
    )
