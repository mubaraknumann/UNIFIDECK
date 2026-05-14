"""UMU runtime lifecycle — bootstraps the runtime cache and retries launches on recoverable exit codes."""

from __future__ import annotations
import asyncio
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path
logger = logging.getLogger(__name__)
UMU_CACHE_DIR = Path("~/.local/share/umu").expanduser()
_RECOVERABLE_CODES = {2, 74}
def cleanup_umu_runtime_cache() -> None:
    """Wipe the UMU runtime cache (``~/.local/share/umu/``).

    Targets the ``steamrt3`` dir, the ``compatibilitytool.vdf``
    shim, and the ``.ref`` marker. Failures are silent. Called
    before retrying a recoverable-error launch.
    """
    targets = [
        UMU_CACHE_DIR / "steamrt3",
        UMU_CACHE_DIR / "compatibilitytool.vdf",
        UMU_CACHE_DIR / ".ref",
    ]
    for target in targets:
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            try:
                target.unlink()
            except OSError:
                pass
    logger.info("[launcher.umu] cache cleaned: %s", UMU_CACHE_DIR)
def ensure_umu_runtime_ready() -> None:
    """Pre-create the UMU cache + config dirs and pin env vars.

    Sets ``UMU_LOG=1`` and ``UMU_NO_PROTON=0`` so the runtime
    logs verbosely and uses our Proton selection.
    """
    UMU_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["UMU_LOG"] = "1"
    os.environ["UMU_NO_PROTON"] = "0"
    config_dir = Path("~/.config/umu").expanduser()
    config_dir.mkdir(parents=True, exist_ok=True)

async def run_umu_with_retry(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    max_attempts: int = 2,
    on_start: Callable[[object], None] | None = None,
) -> int:

    """Spawn umu-run with automatic retry on recoverable codes (2, 74).

    Each attempt creates a new subprocess in its own process
    group. Codes 2 / 74 trigger a cache wipe + retry up to
    ``max_attempts`` times; anything else (success or other
    failure) is returned immediately. ``on_start`` is invoked
    with the live subprocess after spawn — failures in the
    callback are logged but not raised.

    Args:
        argv: argv list for ``asyncio.create_subprocess_exec``.
        env: Environment dict for the subprocess.
        cwd: Optional working directory.
        max_attempts: Max attempts on recoverable failure.
        on_start: Optional callback receiving the subprocess.

    Returns:
        Final subprocess exit code.
    """
    last_rc = 1
    for attempt in range(1, max_attempts + 1):
        logger.info(
            "[launcher.umu] run attempt %d/%d: %s",
            attempt, max_attempts, argv[:3],
        )
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            cwd=str(cwd) if cwd else None,
            stdout=None,
            stderr=None,
            start_new_session=True,
        )
        if on_start is not None:
            try:
                on_start(proc)
            except Exception:
                logger.exception("[launcher.umu] on_start callback failed")
        rc = await proc.wait()
        last_rc = rc
        logger.info("[launcher.umu] attempt %d exit code: %d", attempt, rc)
        if rc == 0:
            return 0
        if rc in _RECOVERABLE_CODES and attempt < max_attempts:
            logger.warning(
                "[launcher.umu] recoverable rc=%d, wiping cache and retrying",
                rc,
            )
            cleanup_umu_runtime_cache()
            continue
        return rc
    return last_rc