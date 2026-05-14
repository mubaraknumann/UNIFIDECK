"""
UPC launch environment builder — assembles the env dict for wine-running UPC.

OP-56c | py_modules/unifideck/stores/ubisoft/installer/launch_env.py

Two thin dataclasses (``UbisoftInstallerLaunchEnv``,
``UbisoftLauncherLaunchEnv``) describe the environment variables and
Wine prefix configuration needed by the installer launcher and the
game launcher respectively.

The split between installer/game env exists because the installer needs
``WINEDLLOVERRIDES=mshtml=`` to bypass the embedded IE component, while
games need ``DXVK_*`` overrides for Wine D3D — keeping the two envs
separate avoids leaking installer-only overrides into game launches.
"""

from __future__ import annotations
from dataclasses import dataclass


@dataclass
class _UpcLaunchEnv:
    """Resolved launch environment for spawning UPC under Proton.

    Built once per UPC spawn so the caller doesn't have to
    carry four separate values around.

    Attributes:
        upc_path: Absolute path to ``upc.exe`` inside the prefix
            (Windows-style path).
        umu_run: Path to the ``umu-run`` wrapper script.
        python_bin: Python interpreter feeding umu-run.
        env: Subprocess environment dict (GAMEID, STORE,
            STEAM_COMPAT_*, PROTON_VERB, …).
    """

    upc_path: str
    umu_run: str
    python_bin: str
    env: dict[str, str]


class UpcLaunchEnvBuildError(Exception):
    """Raised when the UPC launch environment can't be built.

    Carries a stable ``error_code`` (e.g. ``"upc_exe_missing"``,
    ``"prefix_uninitialised"``) that the caller surfaces to the
    UI via a localized toast.
    """

    def __init__(self, error_code: str) -> None:
        """Construct with a stable, UI-friendly error code.

        Args:
            error_code: Stable identifier surfaced to the UI
                (e.g. ``"upc_exe_missing"``).
        """
        super().__init__(error_code)
        self.error_code = error_code
