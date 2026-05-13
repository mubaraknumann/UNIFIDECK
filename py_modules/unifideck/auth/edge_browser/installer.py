"""Flatpak-based Microsoft Edge installer + permission overrides.

OP-15c7 | py_modules/unifideck/auth/edge_browser/installer.py

Installs Edge via Flatpak when missing. Pipeline:

1. Verify ``flatpak`` is on PATH;
2. Skip if Edge already present;
3. Add the ``flathub`` remote at user scope if not
   already configured;
4. Capture the current default browser
   (``xdg-settings``);
5. Run ``flatpak install`` for
   ``com.microsoft.Edge``;
6. Restore the previous default browser (Flatpak's
   install sometimes hijacks it);
7. Poll until the Edge binary is callable;
8. Apply the udev override
   (``--filesystem=/run/udev:ro``) so Edge can see
   gamepads.

Every step has a typed error return so the frontend
can show a precise message (i18n keys like
``microsoft.flatpakNotFound`` /
``microsoft.browserInstallFailed`` /
``microsoft.edgeInstallTimeout``).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .detection import find_edge_cmd, flatpak_remote_names, is_edge_installed

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_FLATPAK_APPS = ("com.microsoft.Edge",)
_EDGE_FLATPAK_APP = "com.microsoft.Edge"
_FLATHUB_REMOTE = "flathub"
_FLATHUB_REMOTE_URL = "https://dl.flathub.org/repo/flathub.flatpakrepo"
_NATIVE_BINS = ("microsoft-edge", "microsoft-edge-stable")


class EdgeInstaller:
    """Stateful Edge installer with a callback for env sanitisation."""

    def __init__(self, clean_env_fn: Callable[[], dict]) -> None:
        """Bind the env-builder callback.

        The callback is invoked on every subprocess
        call so the sandbox-cleaning logic from
        ``env.clean_env`` can be reused without
        coupling this module to it directly.

        Args:
            clean_env_fn: callable returning a
                sanitised env dict.
        """
        self._clean_env = clean_env_fn

    def ensure_controller_permissions(self) -> bool:
        """Apply ``flatpak override --filesystem=/run/udev:ro`` for Edge.

        Without this override, Edge inside Flatpak
        can't see ``/run/udev``, which means the
        Gamepad API returns no controllers. The
        override grants read-only access.

        Idempotent: checks the override file first
        and skips re-applying if ``/run/udev`` is
        already listed.

        Args:
            (no args)

        Returns:
            True on success or already-applied.
        """
        if not shutil.which("flatpak"):
            return False
        overrides_path = Path(
            f"~/.local/share/flatpak/overrides/{_EDGE_FLATPAK_APP}",
        ).expanduser()
        try:
            if overrides_path.is_file():
                with overrides_path.open() as fh:
                    if "/run/udev" in fh.read():
                        logger.debug(
                            "[Edge] Edge udev override already present",
                        )
                        return True
        except OSError:
            pass
        logger.info(
            "[Edge] Applying flatpak /run/udev:ro override for controller support",
        )
        try:
            proc = subprocess.run(
                [
                    "flatpak",
                    "--user",
                    "override",
                    "--filesystem=/run/udev:ro",
                    _EDGE_FLATPAK_APP,
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if proc.returncode == 0:
                logger.info(
                    "[Edge] Edge udev override applied successfully",
                )
                return True
            stderr = proc.stderr.decode(
                "utf-8",
                errors="replace",
            )[:200]
            logger.warning(
                "[Edge] Edge udev override failed: %s",
                stderr,
            )
            return False
        except Exception as exc:
            logger.warning(
                "[Edge] Edge udev override error: %s",
                exc,
            )
            return False

    def _flatpak_remote_names(self, scope: str) -> set[str]:
        """Forward to ``detection.flatpak_remote_names`` with our env callback.

        Args:
            scope: ``"--user"`` or ``"--system"``.

        Returns:
            Set of remote names.
        """
        return flatpak_remote_names(self._clean_env, scope)

    def find_cmd(self) -> list[str] | None:
        """Forward to ``detection.find_edge_cmd`` with our env callback.

        Returns:
            Argv prefix or ``None``.
        """
        return find_edge_cmd(self._clean_env)

    @property
    def is_installed(self) -> bool:
        """Property wrapper around ``detection.is_edge_installed``.

        Returns:
            True if any Edge form is detected.
        """
        return is_edge_installed(self._clean_env)

    async def _ensure_user_flathub_remote(self) -> bool:
        """Add the ``flathub`` remote at user scope if it's not already there.

        Three-arm:

        * Already present → return True;
        * Subprocess exception → WARN log + return
          False;
        * Subprocess non-zero exit → WARN log +
          return False.

        ``--if-not-exists`` makes the actual ``flatpak
        remote-add`` idempotent on the flatpak side
        too.

        Returns:
            True if the remote is configured.
        """
        if _FLATHUB_REMOTE in flatpak_remote_names(
            self._clean_env,
            "--user",
        ):
            return True
        logger.info(
            "[Edge] Adding user flathub remote for browser installation",
        )
        try:
            proc = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    [
                        "flatpak",
                        "remote-add",
                        "--if-not-exists",
                        "--user",
                        _FLATHUB_REMOTE,
                        _FLATHUB_REMOTE_URL,
                    ],
                    capture_output=True,
                    timeout=60,
                    env=self._clean_env(),
                    check=False,
                ),
            )
        except Exception as e:
            logger.warning(
                "[Edge] Could not add user flathub remote: %s",
                e,
            )
            return False
        if proc.returncode != 0:
            stderr = proc.stderr.decode(
                "utf-8",
                errors="replace",
            )[:200]
            logger.warning(
                "[Edge] Adding user flathub remote failed: %s",
                stderr,
            )
            return False
        return _FLATHUB_REMOTE in flatpak_remote_names(self._clean_env, "--user")

    def _get_default_browser(self) -> str | None:
        """Read the current default browser via ``xdg-settings``.

        Returns ``None`` on any failure (xdg-settings
        absent, command failure, empty output).

        Returns:
            Default browser id, or ``None``.
        """
        try:
            result = subprocess.run(
                ["xdg-settings", "get", "default-web-browser"],
                capture_output=True,
                text=True,
                timeout=5,
                env=self._clean_env(),
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def _restore_default_browser(self, original: str | None) -> None:
        """Reset the default browser to ``original`` if it changed during install.

        ``flatpak install`` for ``com.microsoft.Edge``
        sometimes registers Edge as the default
        browser as a side effect. This method
        detects the change and reverts it.

        ``None`` input (we never captured one) or
        unchanged default → no-op. Failures log at
        DEBUG only — restoring the default isn't
        critical.

        Args:
            original: previous default browser id.
        """
        if not original:
            return
        try:
            current = subprocess.run(
                ["xdg-settings", "get", "default-web-browser"],
                capture_output=True,
                text=True,
                timeout=5,
                env=self._clean_env(),
                check=False,
            ).stdout.strip()
            if current != original:
                subprocess.run(
                    [
                        "xdg-settings",
                        "set",
                        "default-web-browser",
                        original,
                    ],
                    capture_output=True,
                    timeout=5,
                    env=self._clean_env(),
                    check=False,
                )
                logger.info(
                    "[Edge] Restored default browser to %s",
                    original,
                )
        except Exception as e:
            logger.debug(
                "[Edge] Could not restore default browser: %s",
                e,
            )

    async def install(self) -> dict[str, Any]:
        """Run the full install pipeline; return a typed result dict.

        Result shape:

        * ``{"success": True, "message": "<i18n>"}``
          — installed or already-present;
        * ``{"success": False, "error": "<i18n>"}`` —
          failure with i18n key for the UI.

        Errors covered:

        * ``microsoft.flatpakNotFound`` — flatpak
          binary missing;
        * ``microsoft.browserInstallFailed`` —
          generic failure (remote add or install
          step);
        * ``microsoft.edgeInstallTimeout`` — the
          5-minute install timeout fired.

        Success path:

        1. Capture default browser;
        2. ``flatpak install``;
        3. Restore default browser;
        4. Poll for the binary up to 10 s;
        5. Apply udev override.

        Returns:
            Typed result dict.
        """
        if not shutil.which("flatpak"):
            return {"success": False, "error": "microsoft.flatpakNotFound"}
        if self.is_installed:
            return {
                "success": True,
                "message": "microsoft.browserAlreadyInstalled",
            }
        if not await self._ensure_user_flathub_remote():
            return {
                "success": False,
                "error": "microsoft.browserInstallFailed",
            }
        original_browser = self._get_default_browser()
        logger.info(
            "[Edge] Attempting to install Microsoft Edge via flatpak...",
        )
        try:
            proc = await self._run_flatpak_install()
            if proc.returncode == 0:
                logger.info(
                    "[Edge] Microsoft Edge installed successfully",
                )
                self._restore_default_browser(original_browser)
                await self._wait_for_edge_ready()
                self.ensure_controller_permissions()
                return {
                    "success": True,
                    "message": "microsoft.browserInstalled",
                }
            stderr = proc.stderr.decode(
                "utf-8",
                errors="replace",
            )[:200]
            logger.warning(
                "[Edge] Microsoft Edge install failed: %s",
                stderr,
            )
            return {
                "success": False,
                "error": "microsoft.browserInstallFailed",
            }
        except subprocess.TimeoutExpired:
            logger.warning("[Edge] Microsoft Edge install timed out")
            return {
                "success": False,
                "error": "microsoft.edgeInstallTimeout",
            }
        except Exception as e:
            logger.warning(
                "[Edge] Microsoft Edge install error: %s",
                e,
            )
            return {
                "success": False,
                "error": "microsoft.browserInstallFailed",
            }

    async def _run_flatpak_install(
        self,
    ) -> subprocess.CompletedProcess[bytes]:
        """Execute the actual ``flatpak install`` command in a thread.

        Uses ``run_in_executor`` because
        ``subprocess.run`` blocks. 300 s timeout is
        generous — Edge is large and slow on first
        download.

        Returns:
            ``CompletedProcess`` (caller inspects
            ``returncode`` + ``stderr``).
        """
        return await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                [
                    "flatpak",
                    "install",
                    "--user",
                    "--noninteractive",
                    "-y",
                    _FLATHUB_REMOTE,
                    _EDGE_FLATPAK_APP,
                ],
                capture_output=True,
                timeout=300,
                env=self._clean_env(),
                check=False,
            ),
        )

    async def _wait_for_edge_ready(self) -> None:
        """Poll up to 10 s for ``find_cmd`` to return non-``None``.

        Flatpak's install completes before the
        binary is fully registered (especially the
        first time, when the wrapper script is being
        generated). This poll gives it time without
        a hard sleep.

        Returns silently after 10 s — the caller's
        next launch attempt will surface the
        problem if it persists.
        """
        for _ in range(10):
            if self.find_cmd() is not None:
                return
            await asyncio.sleep(1)
