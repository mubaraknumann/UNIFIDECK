"""JSON load + atomic write — the persistence primitives for the config layer.

OP-10b | py_modules/unifideck/config/config_persistence.py

Two functions used by ``ConfigManager`` to read/write
the defaults and user-override files.

* ``load_json_layer`` tolerates every read failure
  (missing file, bad JSON, wrong root type) by
  returning an empty dict — config layers degrade
  gracefully when one layer is broken.
* ``atomic_write_json`` uses the
  ``tempfile.mkstemp + fsync + replace + chmod``
  pattern: torn writes are impossible, post-write
  permissions are pinned to 0o600 (or caller-supplied
  mode).

Underscore-prefixed top-level keys are stripped from
loaded files — convention for in-file comments
(``"_comment": "this is a comment"``).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_json_layer(path: Path) -> dict[str, Any]:
    """Read + parse one JSON config layer, returning ``{}`` on any failure.

    Four-arm failure tolerance:

    1. Falsy / non-existent path → ``{}``.
    2. JSON / OS error → log at WARN + ``{}``.
    3. Non-dict root → log at WARN + ``{}``.
    4. Otherwise → return dict with ``_*`` keys
       stripped (in-file comment convention).

    The strip-underscore step is one-level only —
    nested keys retain their leading underscores
    (they're rare; not worth recursive walking).

    Args:
        path: file path to load.

    Returns:
        Parsed dict (possibly empty).
    """
    if not path or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(
            "[config_persistence] %s unreadable (%s): %s",
            path,
            type(e).__name__,
            e,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "[config_persistence] %s is not a JSON object (got %s) — ignoring",
            path,
            type(data).__name__,
        )
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


def atomic_write_json(path: Path, data: dict[str, Any], mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` atomically with the given mode.

    Pipeline:

    1. Ensure parent dir exists;
    2. ``mkstemp`` a sibling temp file with hidden
       prefix (so it doesn't pollute directory
       listings if anything goes wrong);
    3. Dump JSON sorted by key (deterministic output
       for diffability);
    4. ``flush + fsync`` for durability;
    5. ``chmod`` to the target mode (before rename
       so the file is never world-readable);
    6. ``replace`` over the target (atomic);
    7. On any failure in ``try``, attempt to unlink
       the leftover tmp file in ``finally``.

    ``tmp_path = None`` after successful rename
    short-circuits the cleanup — only unfinished
    writes get the cleanup pass.

    Args:
        path: target file path.
        data: serialisable dict.
        mode: octal file mode (default ``0o600``).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_init = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path: str | None = tmp_path_init
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        Path(tmp_path_init).chmod(mode)
        Path(tmp_path_init).replace(path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass
