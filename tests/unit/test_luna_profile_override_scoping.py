"""Property test for the Luna flatpak-override scoping (task 3.2).

Exercises the PURE path computation in
``unifideck.stores.luna.luna_profile_override.compute_override_filesystem_args``
across a wide range of generated profile-dir / plugin-state-dir layouts —
siblings, deeply nested profiles *inside* the state dir, the profile being
the state dir itself, and the profile being an ancestor of the state dir.

The invariant under test (design **Property 4**, Req 8.2/8.3/6.3/6.4):

    the computed ``--filesystem`` override set grants the Luna profile dir
    (read-write) **plus** the fixed ``/run/udev:ro`` and NOTHING ELSE — it
    never grants the plugin state dir, never grants an ancestor of the
    state dir, and never grants any state-dir path outside the profile
    subtree. When the only safe answer would be to grant the state dir (or
    an ancestor of it — i.e. the profile is the state dir or a parent of
    it), the computation must REFUSE rather than widen the grant.

This is a pure string/path computation with no I/O, matching the design's
Property 4 strategy note.
"""
from __future__ import annotations

import os
from pathlib import PurePosixPath

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from unifideck.stores.luna.luna_profile_override import (
    UDEV_FILESYSTEM_ARG,
    LunaProfileError,
    compute_override_filesystem_args,
)

# A path segment that is a well-formed directory name: no separators, no
# "." / ".." traversal, no leading/trailing whitespace that os.path.normpath
# would collapse unpredictably.
_SEGMENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
    min_size=1,
    max_size=12,
).filter(lambda s: s not in {".", ".."})

# Absolute POSIX directory paths built from 1..5 clean segments, e.g.
# "/home/deck/.local/share/unifideck". Kept absolute so normpath is stable
# and relative_to() comparisons are meaningful.
_ABS_DIR = st.lists(_SEGMENT, min_size=1, max_size=5).map(
    lambda parts: "/" + "/".join(parts),
)


def _normalise(path: str) -> str:
    """Mirror the module's own normalisation for assertion arithmetic."""
    return os.path.normpath(path)


def _grants(args: list[str]) -> list[str]:
    """Extract the granted paths (strip the ``--filesystem=`` prefix and any
    ``:ro`` / ``:create`` access suffix)."""
    out: list[str] = []
    for arg in args:
        assert arg.startswith("--filesystem="), arg
        value = arg[len("--filesystem="):]
        # Drop a trailing access mode (":ro", ":rw", ":create", ...).
        path = value.rsplit(":", 1)[0] if ":" in value else value
        out.append(path)
    return out


def _is_same_or_ancestor(candidate: str, target: str) -> bool:
    """True iff ``candidate`` is ``target`` or an ancestor of ``target``."""
    if candidate == target:
        return True
    try:
        PurePosixPath(target).relative_to(PurePosixPath(candidate))
    except ValueError:
        return False
    return True


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(profile=_ABS_DIR, state=_ABS_DIR)
def test_flatpak_override_scoped_to_profile_only(profile: str, state: str) -> None:
    """Feature: amazon-luna-streaming, Property 4: Flatpak-override scoping.

    For any configured Luna profile dir and plugin state-dir layout
    (including a profile nested inside the state dir), the computed set of
    flatpak ``--filesystem`` paths grants the Luna profile dir (plus
    ``/run/udev:ro``) and never grants any state-dir path outside the
    profile subtree, and never broadens to a parent of the state dir.

    Validates: Requirements 8.2, 8.3, 6.3, 6.4
    """
    norm_profile = _normalise(profile)
    norm_state = _normalise(state)

    try:
        args = compute_override_filesystem_args(profile, state)
    except LunaProfileError as exc:
        # The one safe refusal: the profile would grant the state dir itself
        # or a parent of it. Refusing is correct — it must NOT widen.
        assert exc.kind == "profile_permission"
        assert _is_same_or_ancestor(norm_profile, norm_state), (
            "computation refused but the profile was NOT an ancestor of "
            f"the state dir: profile={norm_profile!r} state={norm_state!r}"
        )
        return

    # The udev read-only grant is always present, exactly once, verbatim.
    assert args.count(UDEV_FILESYSTEM_ARG) == 1, args

    grants = _grants(args)

    # Exactly two grants: udev (ro) and the profile dir — nothing broader.
    assert len(grants) == 2, grants
    assert "/run/udev" in grants
    non_udev = [g for g in grants if g != "/run/udev"]
    assert non_udev == [norm_profile], non_udev

    # The sole non-udev grant is the profile dir itself.
    granted = non_udev[0]
    assert granted == norm_profile

    # It must never be the state dir nor an ancestor of the state dir:
    # that would expose the cleartext-credentials file living in the state
    # dir but outside the profile subtree.
    assert not _is_same_or_ancestor(granted, norm_state), (
        f"grant {granted!r} is the state dir or an ancestor of it "
        f"(state={norm_state!r}) — the override widened unsafely"
    )

    # And every granted path (other than udev) must stay within the profile
    # subtree — i.e. no state-dir path outside the profile is ever granted.
    for g in non_udev:
        assert _is_same_or_ancestor(norm_profile, g), (
            f"grant {g!r} escaped the Luna profile subtree {norm_profile!r}"
        )
