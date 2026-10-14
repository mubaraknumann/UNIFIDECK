"""Luna browser-game profile directory + narrowly scoped flatpak override.

Two concerns live here, deliberately split so the path computation is a
**pure, I/O-free function** the Property 4 test (task 3.2) can exercise
directly, while the side-effecting mkdir/apply/permission-check sits
behind it:

- :func:`compute_override_filesystem_args` — PURE. Given the resolved Luna
  profile dir and the plugin state dir, it returns the exact
  ``--filesystem=...`` argument set a ``flatpak --user override`` call
  would carry. The invariant it guarantees (design **Property 4**,
  Req 8.2/8.3/6.3/6.4): the set grants the Luna profile dir **plus** the
  fixed ``/run/udev:ro`` and nothing else — never a plugin-state path
  outside the profile subtree, never a parent of the state dir. The Luna
  profile may legitimately sit *inside* the state dir
  (``WEB_GAME_PROFILE_DIR`` is ``~/.local/share/unifideck/edge-webgames``);
  the override must still grant only that subtree, because the state dir
  also holds a cleartext-credentials file.

- :class:`LunaProfileOverride` — the side-effecting wrapper. It resolves
  the profile dir (defaulting to ``WEB_GAME_PROFILE_DIR`` only when
  ``luna.profile_dir`` is unset, Req 12.8), creates it if absent (Req 8.1),
  checks writability (Req 1.9), and applies the flatpak override through
  the reused Edge flatpak installer path (Req 8.2). It never hand-rolls a
  bespoke flatpak CLI call for the udev grant — that reuses
  :meth:`EdgeInstaller.ensure_controller_permissions`.

Errors are surfaced as :class:`LunaProfileError` naming the offending path:
a ``profile_permission`` error when the dir is not writable (and the
browser is *not* opened, Req 1.9), and a ``profile_not_persisted`` error
naming the missing override when the dir is absent after the browser
started (Req 8.4).
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from unifideck.auth.edge_browser.detection import find_edge_cmd
from unifideck.auth.edge_browser.edge import WEB_GAME_PROFILE_DIR
from unifideck.auth.edge_browser.env import clean_env

logger = logging.getLogger(__name__)

#: The plugin state directory. The Luna profile dir may live inside it
#: (the shared ``WEB_GAME_PROFILE_DIR`` does), but the flatpak override must
#: never widen to grant the state dir itself — it holds a
#: cleartext-credentials file (design Security note).
PLUGIN_STATE_DIR = str(Path("~/.local/share/unifideck").expanduser())

#: The fixed, read-only udev grant every Luna override carries so the
#: sandboxed browser can identify gamepads (Req 6.3). ``devices=all`` in the
#: flatpak manifest exposes ``/dev/*`` nodes only; ``/run/udev`` metadata
#: needs this separate filesystem override.
UDEV_FILESYSTEM_ARG = "--filesystem=/run/udev:ro"


class LunaProfileError(Exception):
    """A Luna profile/override failure that names the offending path.

    ``kind`` is one of ``"profile_permission"`` (dir not writable at
    connect — do not open the browser, Req 1.9) or
    ``"profile_not_persisted"`` (dir absent after the browser started,
    naming the missing override, Req 8.4).
    """

    def __init__(self, kind: str, path: str, message: str) -> None:
        """Build the error carrying its kind and the named path."""
        super().__init__(message)
        self.kind = kind
        self.path = path


def resolve_profile_dir(configured: str | None) -> str:
    """The Luna profile dir, defaulting to ``WEB_GAME_PROFILE_DIR``.

    The shared browser-game profile is used only when ``luna.profile_dir``
    is unset (Req 12.8); any configured value wins. The result is an
    absolute, ``~``-expanded path with no trailing separator so it compares
    cleanly in :func:`compute_override_filesystem_args`.
    """
    raw = configured if configured else WEB_GAME_PROFILE_DIR
    return _normalise(raw)


def compute_override_filesystem_args(
    profile_dir: str,
    state_dir: str = PLUGIN_STATE_DIR,
) -> list[str]:
    """Compute the flatpak ``--filesystem`` arg set for the Luna profile.

    PURE: no filesystem access, no subprocess — just path arithmetic, so
    the Property 4 test can drive it with generated layouts. The returned
    set grants the Luna profile dir (read-write) plus the fixed
    ``/run/udev:ro`` and **nothing else**.

    The invariant (design Property 4): the only non-udev grant is the
    profile dir itself. It is never widened to the state dir or any
    ancestor of it, even when the profile sits inside the state dir — the
    grant stays scoped to the profile subtree so the cleartext-credentials
    file elsewhere in the state dir is never exposed.

    Args:
      profile_dir: The resolved Luna profile dir (see
        :func:`resolve_profile_dir`).
      state_dir: The plugin state dir the grant must never widen to.

    Returns:
      The ``--filesystem=...`` args, udev first, in a stable order.

    """
    profile = _normalise(profile_dir)
    state = _normalise(state_dir)

    # Guard the one way the computation could leak: granting a path that is
    # an ancestor of (or equal to) the state dir would expose the whole
    # state dir, including the credentials file. The profile dir is allowed
    # to be *inside* the state dir, but never a parent of or equal to it.
    if _is_same_or_ancestor(profile, state):
        msg = (
            f"refusing to compute a Luna flatpak override that would grant "
            f"the plugin state dir or an ancestor of it: profile={profile!r} "
            f"state={state!r}"
        )
        raise LunaProfileError("profile_permission", profile, msg)

    return [UDEV_FILESYSTEM_ARG, f"--filesystem={profile}"]


@dataclass(frozen=True)
class LunaProfileOverride:
    """Side-effecting profile-dir + flatpak-override manager.

    Wraps the pure :func:`compute_override_filesystem_args` with the mkdir,
    writability check, and flatpak-apply the pure helper deliberately omits.
    Constructed from the resolved profile dir so the launcher subprocess and
    the plugin backend can both build it from ``LunaConfig`` alone.
    """

    profile_dir: str
    state_dir: str = PLUGIN_STATE_DIR
    #: The configured Luna browser candidates (``stores.luna.browser_candidates``).
    #: The flatpak override MUST target the browser that actually launches —
    #: ``LunaBrowser`` resolves it from these candidates (Chrome first on the
    #: Deck), NOT the Edge default. Empty falls back to the Edge resolution.
    browser_candidates: tuple[str, ...] = ()

    @classmethod
    def from_config(
        cls,
        configured_profile_dir: str | None,
        browser_candidates: list[str] | None = None,
    ) -> LunaProfileOverride:
        """Build from the configured ``luna.profile_dir`` + browser candidates."""
        return cls(
            profile_dir=resolve_profile_dir(configured_profile_dir),
            browser_candidates=tuple(browser_candidates or ()),
        )

    def filesystem_args(self) -> list[str]:
        """The computed ``--filesystem`` arg set (delegates to the pure fn)."""
        return compute_override_filesystem_args(self.profile_dir, self.state_dir)

    def ensure_profile_dir(self) -> str:
        """Create the Luna profile dir if absent and confirm it is writable.

        Returns the resolved profile dir on success. Raises
        :class:`LunaProfileError` (``profile_permission``) naming the path
        when the dir cannot be created or is not writable, so the caller
        does **not** open the browser (Req 1.9, 8.1).
        """
        path = Path(self.profile_dir)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            msg = f"Luna profile dir is not writable: {self.profile_dir} ({exc})"
            logger.warning("[LunaProfile] %s", msg)
            raise LunaProfileError(
                "profile_permission", self.profile_dir, msg,
            ) from exc
        if not os.access(self.profile_dir, os.W_OK):
            msg = f"Luna profile dir is not writable: {self.profile_dir}"
            logger.warning("[LunaProfile] %s", msg)
            raise LunaProfileError(
                "profile_permission", self.profile_dir, msg,
            )
        return self.profile_dir

    def apply_flatpak_override(self) -> bool:
        """Apply the narrowly scoped flatpak override for the Luna browser.

        Resolves the configured flatpak browser app id and applies the
        computed ``--filesystem`` grants via ``flatpak --user override``,
        reusing the Edge installer's env scrubbing. A native (non-flatpak)
        browser needs no override and returns ``True`` as a no-op.

        Returns ``True`` when the override is applied (or unnecessary),
        ``False`` on a flatpak error.
        """
        app_id = _resolve_flatpak_app_id(self.browser_candidates)
        if app_id is None:
            logger.debug(
                "[LunaProfile] browser is not a flatpak; no override needed",
            )
            return True
        args = self.filesystem_args()
        logger.info(
            "[LunaProfile] applying flatpak override for %s: %s",
            app_id, " ".join(args),
        )
        try:
            proc = subprocess.run(
                ["flatpak", "--user", "override", *args, app_id],
                capture_output=True,
                timeout=30,
                env=clean_env(),
                check=False,
            )
        except Exception as exc:
            logger.warning("[LunaProfile] flatpak override error: %s", exc)
            return False
        if proc.returncode == 0:
            return True
        stderr = proc.stderr.decode("utf-8", errors="replace")[:200]
        logger.warning("[LunaProfile] flatpak override failed: %s", stderr)
        return False

    def verify_persisted(self) -> None:
        """Confirm the profile dir exists after the browser started.

        A flatpak browser silently fails to create a ``--user-data-dir`` it
        has no filesystem permission for, so an absent dir here means the
        override never landed. Raises :class:`LunaProfileError`
        (``profile_not_persisted``) naming the missing override (Req 8.4).
        """
        if not Path(self.profile_dir).is_dir():
            missing = f"--filesystem={self.profile_dir}"
            msg = (
                f"Luna profile dir absent after browser start; the flatpak "
                f"override {missing!r} did not take effect"
            )
            logger.warning("[LunaProfile] %s", msg)
            raise LunaProfileError(
                "profile_not_persisted", self.profile_dir, msg,
            )


def _resolve_flatpak_app_id(
    browser_candidates: tuple[str, ...] = (),
) -> str | None:
    """The flatpak app id of the browser Luna actually launches, or ``None``.

    Resolves the SAME browser ``LunaBrowser.find_cmd`` does — from the
    configured ``browser_candidates`` (Chrome first on the Deck) via
    :func:`resolve_browser_cmd` — so the ``--filesystem`` grant lands on the
    browser that opens the kiosk, not on Edge. A path mismatch here is why
    the sandboxed browser silently wrote its profile into its own sandbox
    default instead of the Luna profile dir (cookies never persisted). Falls
    back to the shared Edge detection only when no candidates are configured.
    """
    if browser_candidates:
        from unifideck.stores.luna.luna_browser import resolve_browser_cmd
        cmd = resolve_browser_cmd(list(browser_candidates))
    else:
        cmd = find_edge_cmd(clean_env)
    if cmd and len(cmd) >= 3 and cmd[0] == "flatpak" and cmd[1] == "run":
        return cmd[2]
    return None


def _normalise(path: str) -> str:
    """Absolute, ``~``-expanded path with no trailing separator."""
    return os.path.normpath(str(Path(path).expanduser()))


def _is_same_or_ancestor(candidate: str, target: str) -> bool:
    """Whether ``candidate`` is ``target`` itself or an ancestor of it.

    Uses path-component containment (not string prefix) so that
    ``/a/b`` is correctly judged *not* an ancestor of ``/a/bc``.
    """
    if candidate == target:
        return True
    try:
        Path(target).relative_to(candidate)
    except ValueError:
        return False
    return True
