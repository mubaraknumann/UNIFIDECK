"""
UPC binary resolution — find the right executable for the install in a prefix.

OP-55d | py_modules/unifideck/stores/ubisoft/binaries.py

``UbisoftBinaryResolver`` locates the Ubisoft Connect / UPC executable
inside a Wine prefix. Modern UPC ships ``UbisoftConnect.exe`` (Electron-
based UI) and a legacy ``upc.exe`` (the original launcher); newly
installed UPC may have only ``UbisoftConnect.exe`` while older installs
have both — the resolver picks the modern one when available and falls
back to the legacy binary otherwise.

It also exposes helpers to:

* probe a prefix for *any* working UPC binary;
* validate a candidate path against config-declared relative paths;
* enumerate every game-install binary candidate found in the prefix's
  ``games/`` sub-directory.

Returns ``str`` paths to maintain compatibility with subprocess callers
that pass paths directly to ``proton run``.
"""

from __future__ import annotations
import logging
import os
import shutil
import subprocess
from pathlib import Path
from .config import UbisoftConfig

logger = logging.getLogger(__name__)
_DISPLAY_ENV_VARS = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
    "XAUTHORITY",
)
_PROTON_OFFICIAL_NAMES = (
    "Proton - Experimental",
    "Proton 10.0",
    "Proton 9.0 (Beta)",
)
_STEAM_COMMON_CANDIDATES = (
    str(Path("~") / ".steam" / "steam" / "steamapps" / "common"),
    str(
        Path("~") / ".local" / "share" / "Steam" / "steamapps" / "common",
    ),
    str(Path("~") / ".steam" / "root" / "steamapps" / "common"),
)
_COMPAT_TOOLS_DIR = "~/.local/share/Steam/compatibilitytools.d"


class UbisoftBinaryResolver:
    """Locate Ubisoft Connect / Proton / Python binaries and build subprocess envs.

    Used by every flow that has to spawn a Wine-side process —
    auth, install, launch, registry injection. Returns paths
    as ``str`` since callers pass them to ``proton run``
    directly.
    """

    def __init__(
        self,
        config: UbisoftConfig,
        plugin_dir: str | None,
    ) -> None:
        """Bind the binary resolver to its config snapshot + plugin dir.

        Args:
            config: Frozen ``UbisoftConfig``.
            plugin_dir: Plugin root directory (None when running
                outside the Decky host).
        """
        self._config = config
        self._plugin_dir = plugin_dir

    def find_umu_run(self) -> str | None:
        """Locate the ``umu-run`` wrapper script.

        Search order: bundled under ``<plugin_dir>/bin/umu/umu/`` →
        ``~/.local/share/unifideck/bin/umu/umu/`` → ``/usr/bin``.

        Returns:
            Absolute path string, or ``None`` if no umu-run found.
        """
        candidates: list[str] = []
        if self._plugin_dir:
            candidates.append(
                str(
                    Path(self._plugin_dir) / "bin" / "umu" / "umu" / "umu-run",
                )
            )
        candidates.extend(
            [
                str(
                    Path(
                        "~/.local/share/unifideck/bin/umu/umu/umu-run",
                    ).expanduser()
                ),
                "/usr/bin/umu-run",
            ]
        )
        for path in candidates:
            if Path(path).is_file():
                return path
        logger.warning("[UbisoftBinaryResolver] umu-run not found")
        return None

    def find_proton_path(self) -> str | None:
        """Locate a usable Proton install directory.

        Tries the official Proton names (Experimental, 10.0,
        9.0 Beta) first; falls back to UMU-Proton or GE-Proton
        under ``compatibilitytools.d``.

        Returns:
            Absolute path to the Proton tool dir (not the script),
            or ``None`` if nothing was found.
        """
        official = self._find_official_proton()
        if official is not None:
            return official
        custom = self._find_custom_proton()
        if custom is not None:
            return custom
        logger.warning(
            "[UbisoftBinaryResolver] no Proton found in "
            "steamapps/common or compatibilitytools.d",
        )
        return None

    @staticmethod
    def _find_official_proton() -> str | None:
        """Scan the standard Steam common dirs for the official Proton tools.

        Returns:
            Absolute path to the first match, or ``None``.
        """
        for steam_common_raw in _STEAM_COMMON_CANDIDATES:
            steam_common = Path(steam_common_raw).expanduser()
            for name in _PROTON_OFFICIAL_NAMES:
                candidate = steam_common / name
                if candidate.is_dir():
                    logger.info(
                        "[UbisoftBinaryResolver] using Proton: %s",
                        name,
                    )
                    return str(candidate)
        return None

    @staticmethod
    def _find_custom_proton() -> str | None:
        """Scan ``compatibilitytools.d`` for UMU-Proton then GE-Proton entries.

        UMU-Proton is preferred when present (it's what umu-run
        is designed for). GE-Proton entries are sorted in reverse
        lexicographic order so the newest is picked.

        Returns:
            Absolute path to the chosen tool dir, or ``None``.
        """
        compat_dir = Path(_COMPAT_TOOLS_DIR).expanduser()
        if not compat_dir.is_dir():
            return None
        umu_candidates: list[str] = []
        ge_candidates: list[str] = []
        try:
            for entry in compat_dir.iterdir():
                if not entry.is_dir():
                    continue
                if entry.name.startswith("UMU-Proton"):
                    umu_candidates.append(str(entry))
                elif entry.name.startswith("GE-Proton"):
                    ge_candidates.append(str(entry))
        except OSError as e:
            logger.warning(
                "[UbisoftBinaryResolver] compatibilitytools.d scan failed: %s",
                e,
            )
            return None
        ge_candidates.sort(
            key=lambda p: Path(p).name,
            reverse=True,
        )
        ordered = umu_candidates + ge_candidates
        if not ordered:
            return None
        logger.info(
            "[UbisoftBinaryResolver] using Proton: %s",
            Path(ordered[0]).name,
        )
        return ordered[0]

    @staticmethod
    def find_python() -> str:
        """Locate a Python interpreter (used as the umu-run host).

        Returns:
            Absolute path to ``python3``/``python``, or the literal
            ``"python3"`` if no candidate resolved via PATH.
        """
        for name in ("python3", "python"):
            path = shutil.which(name)
            if path:
                return path
        return "python3"

    @staticmethod
    def proton_family(version_str: str) -> str:
        """Classify a Proton version string into a coarse family.

        Args:
            version_str: Version display string (e.g. ``"GE-Proton9-22"``).

        Returns:
            One of ``umu-proton``, ``ge-proton``, ``experimental``,
            or ``other``. Pure-digit version strings classify as
            ``experimental``.
        """
        v = (version_str or "").lower()
        if "umu-proton" in v:
            return "umu-proton"
        if "ge-proton" in v:
            return "ge-proton"
        if "experimental" in v:
            return "experimental"
        stripped = v.replace(".", "").replace("-", "").replace(" ", "")
        if stripped.isdigit():
            return "experimental"
        return "other"

    def build_umu_env(
        self,
        wineprefix: str,
        gameid: str,
        *,
        proton_path: str | None = None,
        store_game_id: str | None = None,
        steam_window_env: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build the environment dict for spawning UPC via umu-run.

        Always sets HOME, USER, PATH, LANG, XDG_RUNTIME_DIR,
        XDG_DATA_HOME, WINEPREFIX, GAMEID, STORE=ubisoft, and
        PROTON_VERB. PROTONPATH is filled in if Proton can be
        located. Display vars (DISPLAY, WAYLAND_DISPLAY, …) are
        merged in via ``detect_display_env``.

        Args:
            wineprefix: Absolute prefix path.
            gameid: UMU game ID.
            proton_path: Override the resolved Proton path.
            store_game_id: Unused (kept for signature stability).
            steam_window_env: Extra env merged in (typically
                Steam window vars for overlay/input compatibility).

        Returns:
            Environment dict ready for ``subprocess`` / ``asyncio``.
        """
        home = os.environ.get(
            "HOME",
            str(Path("~").expanduser()),
        )
        uid = os.getuid()
        env: dict[str, str] = {
            "HOME": home,
            "USER": os.environ.get("USER", "deck"),
            "PATH": os.environ.get(
                "PATH",
                "/usr/local/bin:/usr/bin:/bin",
            ),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "XDG_RUNTIME_DIR": os.environ.get(
                "XDG_RUNTIME_DIR",
                f"/run/user/{uid}",
            ),
            "XDG_DATA_HOME": os.environ.get(
                "XDG_DATA_HOME",
                str(Path(home) / ".local" / "share"),
            ),
            "WINEPREFIX": wineprefix,
            "GAMEID": gameid,
            "STORE": "ubisoft",
            "PROTON_VERB": "waitforexitandrun",
        }
        if steam_window_env:
            env.update(steam_window_env)
        if proton_path is None:
            proton_path = self.find_proton_path()
        if proton_path:
            env["PROTONPATH"] = proton_path
        env.update(self.detect_display_env())
        return env

    def detect_display_env(self) -> dict[str, str]:
        """Detect or synthesize the X11/Wayland display environment.

        Tries our own env first → falls back to scraping live
        ``steam`` / ``gamescope-session`` processes → finally
        applies Steam Deck defaults (``DISPLAY=:0``, ``XAUTHORITY=~/.Xauthority``).

        Returns:
            Dict containing whichever of DISPLAY / WAYLAND_DISPLAY /
            DBUS_SESSION_BUS_ADDRESS / XAUTHORITY could be resolved.
        """
        result = self._collect_display_env_from_self()
        if result.get("DISPLAY") or result.get("WAYLAND_DISPLAY"):
            return result
        if self._fill_display_env_from_steam(result):
            return result
        self._apply_steam_deck_defaults(result)
        return result

    @staticmethod
    def _collect_display_env_from_self() -> dict[str, str]:
        """Snapshot the display env vars from our own process.

        Returns:
            Dict containing whichever ``_DISPLAY_ENV_VARS`` are set.
        """
        result: dict[str, str] = {}
        for var in _DISPLAY_ENV_VARS:
            val = os.environ.get(var)
            if val:
                result[var] = val
        return result

    def _fill_display_env_from_steam(
        self,
        result: dict[str, str],
    ) -> bool:
        """Scrape ``steam`` / ``gamescope-session`` processes for display env vars.

        Args:
            result: Output dict (mutated in place; existing keys
                are not overwritten).

        Returns:
            True iff DISPLAY or WAYLAND_DISPLAY could be filled in.
        """
        try:
            for proc_name in ("steam", "gamescope-session"):
                for pid in self._pgrep(proc_name):
                    if self._scan_pid_for_display(pid, result):
                        logger.info(
                            "[UbisoftBinaryResolver] display "
                            "env detected from PID %s (%s)",
                            pid,
                            proc_name,
                        )
                        return True
        except Exception as e:
            logger.debug(
                "[UbisoftBinaryResolver] display env detection: %s",
                e,
            )
        return False

    def _scan_pid_for_display(
        self,
        pid: str,
        result: dict[str, str],
    ) -> bool:
        """Read ``/proc/<pid>/environ`` and merge display vars into the result dict.

        Args:
            pid: PID to scan.
            result: Output dict (mutated; existing keys preserved).

        Returns:
            True iff DISPLAY or WAYLAND_DISPLAY ended up populated.
        """
        env_from_proc = self._read_proc_environ(
            pid,
            _DISPLAY_ENV_VARS,
        )
        if not env_from_proc:
            return False
        for k, v in env_from_proc.items():
            result.setdefault(k, v)
        return bool(
            result.get("DISPLAY") or result.get("WAYLAND_DISPLAY"),
        )

    @staticmethod
    def _apply_steam_deck_defaults(
        result: dict[str, str],
    ) -> None:
        """Apply the Steam Deck fallback display env (``DISPLAY=:0`` + Xauthority).

        Args:
            result: Output dict (mutated; existing keys preserved).
        """
        if not result.get("DISPLAY"):
            result["DISPLAY"] = ":0"
            logger.info(
                "[UbisoftBinaryResolver] using fallback DISPLAY=:0",
            )
        xauth = Path("~").expanduser() / ".Xauthority"
        if not result.get("XAUTHORITY") and xauth.is_file():
            result["XAUTHORITY"] = str(xauth)

    @staticmethod
    def _pgrep(process_name: str) -> list[str]:
        """Run ``pgrep -u <uid> -x <name>`` and return matching PIDs.

        Args:
            process_name: Exact process name.

        Returns:
            List of PID strings (empty on subprocess failure).
        """
        try:
            result = subprocess.run(
                [
                    "pgrep",
                    "-u",
                    str(os.getuid()),
                    "-x",
                    process_name,
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            return []
        return [
            line.strip() for line in result.stdout.strip().splitlines() if line.strip()
        ]

    @staticmethod
    def _read_proc_environ(
        pid: str,
        targets: tuple[str, ...],
    ) -> dict[str, str]:
        """Read environment variables from ``/proc/<pid>/environ`` (filtered).

        Args:
            pid: PID to read.
            targets: Tuple of env var names to keep.

        Returns:
            Dict of matching env vars (empty on permission /
            not-found errors).
        """
        try:
            env_path = Path(f"/proc/{pid}/environ")
            env_bytes = env_path.read_bytes()
        except (PermissionError, FileNotFoundError, OSError):
            return {}
        result: dict[str, str] = {}
        for entry in env_bytes.split(b"\x00"):
            decoded = entry.decode("utf-8", errors="replace")
            if "=" not in decoded:
                continue
            k, v = decoded.split("=", 1)
            if k in targets:
                result[k] = v
        return result
