"""
Wine ↔ Linux path conversion — small utility helpers.

OP-57i | py_modules/unifideck/stores/ubisoft/library/wine_path.py

Pure functions that convert between Wine-style paths (``C:\\...``) and
Linux-side paths (``<prefix>/drive_c/...``). Used by the library and
detection modules whenever they read a path out of a UPC config file
(which uses Wine syntax) and need to access it on the Linux side.

The functions are conservative: they refuse to convert paths that
don't look Wine-formatted, and they reject paths that would escape the
prefix root after conversion (security against path-traversal in
malformed config files).
"""

from __future__ import annotations
from pathlib import Path


def wine_path_to_linux(
    wine_path: str,
    prefix_path: str,
) -> str | None:
    """Convert a Wine-style path (``C:\\...``) to a Linux path.

    Recognizes:
      * ``C:`` → ``<prefix>/[pfx/]drive_c/...``
      * ``Z:`` → the real root ``/``
      * other drive letters → resolved via the
        ``dosdevices/X:`` symlink.

    Args:
        wine_path: Wine-formatted path.
        prefix_path: Wine prefix root.

    Returns:
        Resolved Linux path, or ``None`` for malformed input
        or unknown drive letters.
    """
    path = wine_path.replace("\\", "/")
    if len(path) < 2 or path[1] != ":":
        return None
    drive_letter = path[0].upper()
    relative = path[2:].lstrip("/")
    if drive_letter == "Z":
        return _resolve_z_drive(relative)
    if drive_letter == "C":
        return _resolve_c_drive(prefix_path, relative)
    return _resolve_other_drive(
        prefix_path,
        drive_letter,
        relative,
    )


def _resolve_z_drive(relative: str) -> str:
    """Resolve a ``Z:`` Wine path to the real Linux root.

    Args:
        relative: Wine path component after ``Z:``.

    Returns:
        Linux path string (``/`` if relative was empty).
    """
    return "/" + relative if relative else "/"


def _resolve_c_drive(prefix_path: str, relative: str) -> str:
    """Resolve a ``C:`` Wine path under the prefix's ``drive_c``.

    Tries ``pfx/drive_c`` first (modern Proton layout), then
    ``drive_c`` directly (older / non-Proton prefixes). If
    neither exists, returns the modern path anyway.

    Args:
        prefix_path: Wine prefix root.
        relative: Wine path component after ``C:``.

    Returns:
        Linux path string.
    """
    prefix = Path(prefix_path)
    for base in (prefix / "pfx", prefix):
        candidate = base / "drive_c" / relative
        if candidate.exists():
            return str(candidate)
    return str(prefix / "pfx" / "drive_c" / relative)


def _resolve_other_drive(
    prefix_path: str,
    drive_letter: str,
    relative: str,
) -> str | None:
    """Resolve an arbitrary drive letter via the ``dosdevices`` symlinks.

    Args:
        prefix_path: Wine prefix root.
        drive_letter: Uppercase drive letter (D, E, …).
        relative: Wine path component after the drive prefix.

    Returns:
        Linux path string, or ``None`` if no matching symlink.
    """
    drive_name = f"{drive_letter.lower()}:"
    prefix = Path(prefix_path)
    for base in (prefix / "pfx", prefix):
        link_path = base / "dosdevices" / drive_name
        if link_path.is_symlink():
            target = str(link_path.resolve())
            if relative:
                return str(Path(target) / relative)
            return target
    return None
