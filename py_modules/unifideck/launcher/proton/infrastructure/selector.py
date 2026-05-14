"""Proton version selector — finds a compatible Python interpreter and resolves the active Proton install path."""

from __future__ import annotations
import logging
import re
import subprocess
from pathlib import Path
from ...types.errors import DependencyMissingError, ProtonUnavailableError
logger = logging.getLogger(__name__)
PYTHON_CANDIDATES: list[str] = [
    "/usr/bin/python3.13",
    "/usr/bin/python3.12",
    "/usr/bin/python3.11",
    "/usr/bin/python3.10",
    "/usr/bin/python3",
]
ACCEPTED_VERSIONS = {"3.10", "3.11", "3.12", "3.13", "3.14"}
def find_python_3_10_plus() -> Path:
    """Locate a Python interpreter compatible with umu-run.

    Tries each candidate in ``PYTHON_CANDIDATES`` (most recent
    first); each candidate is probed to read its actual
    ``sys.version_info`` and only accepted if in
    ``ACCEPTED_VERSIONS`` (3.10–3.14).

    Returns:
        Path to the chosen interpreter.

    Raises:
        DependencyMissingError: No suitable interpreter found.
    """
    for candidate in PYTHON_CANDIDATES:
        path = Path(candidate)
        if not path.is_file():
            continue
        try:
            out = subprocess.check_output(
                [
                    candidate,
                    "-c",
                    'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")',
                ],
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (subprocess.SubprocessError, OSError):
            continue
        ver = out.decode().strip()
        if ver in ACCEPTED_VERSIONS:
            logger.info("[launcher.proton] python selected: %s (%s)", candidate, ver)
            return path
    raise DependencyMissingError(
        "No Python 3.10+ interpreter found on system",
        context={"tried": PYTHON_CANDIDATES},
    )

STEAM_COMPAT_ROOTS: list[str] = [
    "~/.steam/root/compatibilitytools.d",
    "~/.local/share/Steam/compatibilitytools.d",
]
STEAM_LIBRARY_ROOTS: list[str] = [
    "~/.steam/root/steamapps/common",
    "~/.local/share/Steam/steamapps/common",
]
UNIFIDECK_COMPAT_DIR = "~/.local/share/unifideck/compat-tools"
def resolve_proton_path(tool_id: str) -> Path | None:
    """Resolve the ``proton`` script for one tool ID across known roots.

    Search order: Unifideck's own compat-tools dir → Steam's
    ``compatibilitytools.d`` → Steam library ``common`` dirs.

    Args:
        tool_id: Proton tool identifier (e.g. ``"GE-Proton9-22"``).

    Returns:
        Path to the ``proton`` script, or ``None`` if not found.
    """
    if not tool_id:
        return None
    unifideck_path = Path(UNIFIDECK_COMPAT_DIR).expanduser() / tool_id / "proton"
    if unifideck_path.is_file():
        return unifideck_path
    for root in STEAM_COMPAT_ROOTS:
        candidate = Path(root).expanduser() / tool_id / "proton"
        if candidate.is_file():
            return candidate
    for lib in STEAM_LIBRARY_ROOTS:
        candidate = Path(lib).expanduser() / tool_id / "proton"
        if candidate.is_file():
            return candidate
    return None
def get_unifideck_proton_tool() -> str | None:
    """Read the user's preferred Proton tool from the Unifideck config.

    Reads ``compat.proton_tool`` from
    ``~/.local/share/unifideck/config.json``.

    Returns:
        Tool ID string, or ``None`` if the config is missing,
        unreadable, or the key is empty.
    """
    config_path = Path("~/.local/share/unifideck/config.json").expanduser()
    if not config_path.is_file():
        return None
    try:
        import json
        with config_path.open() as f:
            cfg = json.load(f)
        tool = cfg.get("compat", {}).get("proton_tool", "")
        return tool or None
    except (OSError, ValueError):
        return None
_COMPAT_TOOL_RE = re.compile(
    r'"(?P<app_id>\d+)"\s*\{[^}]*?"name"\s*"(?P<name>[^"]+)"',
    re.S,
)
def get_steam_compat_tool_override(app_id: str) -> str | None:
    """Parse Steam's per-user ``localconfig.vdf`` for a compat-tool override.

    Steam stores per-game compat tool selections in each
    user's ``localconfig.vdf``. Walks every user under
    ``~/.steam/root/userdata/`` and matches the requested
    AppID.

    Args:
        app_id: Steam AppID to look up.

    Returns:
        Tool name from Steam's config, or ``None`` if no
        user has set an override for this AppID.
    """
    if not app_id:
        return None
    userdata = Path("~/.steam/root/userdata").expanduser()
    if not userdata.is_dir():
        return None
    for user_dir in userdata.iterdir():
        cfg = user_dir / "config" / "localconfig.vdf"
        if not cfg.is_file():
            continue
        try:
            content = cfg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _COMPAT_TOOL_RE.finditer(content):
            if m.group("app_id") == app_id:
                return m.group("name")
    return None
def find_any_ge_proton() -> Path | None:
    """Locate the newest installed GE-Proton build as a fallback.

    Scans ``compatibilitytools.d`` roots for ``GE-Proton*``
    directories containing a ``proton`` script, sorts
    lexicographically, and returns the last (newest).

    Returns:
        Path to a GE-Proton ``proton`` script, or ``None``
        if none is installed.
    """
    candidates: list[Path] = []
    for root in STEAM_COMPAT_ROOTS:
        expanded = Path(root).expanduser()
        if not expanded.is_dir():
            continue
        for entry in expanded.iterdir():
            if entry.name.startswith("GE-Proton"):
                proton_script = entry / "proton"
                if proton_script.is_file():
                    candidates.append(proton_script)
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1]

def select_proton_version(
    steam_app_id: str | None = None,
) -> tuple[Path, str]:

    """Pick the Proton script + tool ID to use for this launch.

    Resolution order: per-game Steam override → user's
    Unifideck default → any installed GE-Proton.

    Args:
        steam_app_id: Optional Steam AppID to consult for an
            override.

    Returns:
        Tuple ``(proton_path, tool_id)``.

    Raises:
        ProtonUnavailableError: No usable Proton found at
            any tier.
    """
    tried: list[str] = []
    if steam_app_id:
        steam_tool = get_steam_compat_tool_override(steam_app_id)
        if steam_tool:
            tried.append(f"steam:{steam_tool}")
            path = resolve_proton_path(steam_tool)
            if path:
                logger.info(
                    "[launcher.proton] selected via Steam override: %s",
                    steam_tool,
                )
                return path, steam_tool
    unifideck_tool = get_unifideck_proton_tool()
    if unifideck_tool:
        tried.append(f"unifideck:{unifideck_tool}")
        path = resolve_proton_path(unifideck_tool)
        if path:
            logger.info(
                "[launcher.proton] selected via Unifideck default: %s",
                unifideck_tool,
            )
            return path, unifideck_tool
    fallback = find_any_ge_proton()
    if fallback:
        tool_id = fallback.parent.name
        tried.append(f"fallback:{tool_id}")
        logger.info("[launcher.proton] selected via GE-Proton fallback: %s", tool_id)
        return fallback, tool_id
    raise ProtonUnavailableError(
        "No usable Proton compat tool found",
        context={"tried": tried},
    )