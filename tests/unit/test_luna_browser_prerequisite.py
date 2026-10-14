"""Prerequisite install integration tests for Luna (task 12.2, installer mocked).

Drives :meth:`LunaStore.install_browser` — Luna's half of the Req 9 flatpak
browser-prerequisite flow — through its real body
(:meth:`LunaStore._run_browser_install` → ``EdgeBrowser.install`` under the
600 s ceiling, :meth:`LunaStore._apply_overrides_and_resume` →
:class:`LunaProfileOverride`, and :meth:`LunaStore._has_flatpak`), mocking only
the installer / flatpak-presence / override-apply seams so no real flatpak
runs and no browser is spawned. Follows the existing Luna harness style
(``test_luna_connect_logout.py`` / ``test_luna_retry_recovery_region.py``):
``LunaStore.__new__`` + private-attr injection, a mocked ``EdgeBrowser``, and
an injected ``LunaProfileOverride`` so the whole flow is drivable in-process.

Covers the four acceptance criteria of task 12.2:

* **not-installed → needs_browser** (Req 9.1): :meth:`LunaStore.is_available`
  reports the prerequisite is missing when no supported browser is installed;
* **flatpak absent → flatpak_not_found** (Req 9.6): with no ``flatpak`` CLI,
  nothing is attempted (installer never called) and the pre-install state is
  kept — no overrides applied, no resume;
* **install failure or >600 s → installation_failed** (Req 9.4/9.5): the
  installer's reason is surfaced in ``metadata["reason"]`` and the interrupted
  operation is left in its pre-install state (not resumed);
* **success within 600 s** (Req 9.2–9.4): the Luna profile-dir + ``/run/udev:ro``
  overrides are applied, then the interrupted operation is resumed and its
  result/status is folded back into the prerequisite result.

_Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6_
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from unifideck.core.types import AuthResult, Result
from unifideck.stores.luna import luna_profile_override as override_module
from unifideck.stores.luna.luna_store import (
    BROWSER_INSTALL_BUDGET_SECONDS,
    FLATPAK_NOT_FOUND,
    INSTALLATION_FAILED,
    LUNA_STORE,
    STATUS_KEY,
    LunaStatus,
    LunaStore,
)

# ── Test doubles ─────────────────────────────────────────────────────

#: Opaque Luna profile dir carried through to the override. Never created or
#: written — every filesystem seam is mocked.
_PROFILE_DIR = "/home/deck/.local/share/unifideck/luna"


class _Edge:
    """Stand-in for ``EdgeBrowser``.

    ``is_installed`` is a plain attribute so a test can flip it; ``install``
    is an :class:`AsyncMock` returning the installer's ``{"success": ...}``
    dict so :meth:`LunaStore._run_browser_install` sees the real shape.
    """

    def __init__(
        self,
        *,
        installed: bool,
        install_result: dict[str, Any] | None = None,
        install_exc: BaseException | None = None,
    ) -> None:
        self.is_installed = installed
        if install_exc is not None:
            self.install = AsyncMock(side_effect=install_exc)
        else:
            self.install = AsyncMock(
                return_value=install_result or {"success": True},
            )


class _RecordingOverride:
    """Stand-in for :class:`LunaProfileOverride`.

    Records whether :meth:`apply_flatpak_override` ran (and with which profile
    dir it was built) so a test can assert the Req 9.4 overrides were applied
    only on the success path. ``applies`` controls the apply's return value.
    """

    built_with: list[str | None] = []

    def __init__(self, profile_dir: str, *, applies: bool = True) -> None:
        self.profile_dir = profile_dir
        self._applies = applies
        self.apply_calls = 0

    @classmethod
    def make_factory(
        cls, *, applies: bool = True,
    ) -> Any:
        """A ``from_config`` replacement recording the resolved profile dir."""
        instances: list[_RecordingOverride] = []

        def from_config(
            configured_profile_dir: str | None,
            browser_candidates: Any = None,
        ) -> _RecordingOverride:
            inst = cls(configured_profile_dir or _PROFILE_DIR, applies=applies)
            instances.append(inst)
            return inst

        from_config.instances = instances  # type: ignore[attr-defined]
        return from_config

    def apply_flatpak_override(self) -> bool:
        self.apply_calls += 1
        return self._applies


def _store(
    edge: _Edge,
    *,
    monkeypatch: pytest.MonkeyPatch,
    has_flatpak: bool = True,
    override_applies: bool = True,
) -> tuple[LunaStore, Any]:
    """Build a ``LunaStore`` wired to the fakes, bypassing ``__init__``.

    Only the attributes the install flow touches are set. ``_has_flatpak`` is
    stubbed to the requested answer, and :class:`LunaProfileOverride` is
    replaced at its import site so ``apply`` is observable without a real
    flatpak. Returns the store plus the ``from_config`` factory so a test can
    read how the override was built.
    """
    store = LunaStore.__new__(LunaStore)
    store._edge = edge
    store._luna_config = SimpleNamespace(
        profile_dir=_PROFILE_DIR, browser_candidates=["com.google.Chrome"],
    )
    # is_available now also consults the session; these install/resume tests
    # assume a usable session unless a test overrides it.
    store._session = SimpleNamespace(is_usable=lambda: True)

    monkeypatch.setattr(
        store, "_has_flatpak", lambda: has_flatpak,
    )
    factory = _RecordingOverride.make_factory(applies=override_applies)
    monkeypatch.setattr(
        override_module.LunaProfileOverride, "from_config",
        staticmethod(factory),
    )
    return store, factory


# ── not-installed → needs_browser (Req 9.1) ──────────────────────────


async def test_not_installed_reports_needs_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No supported browser → ``is_available`` is False (needs_browser, Req 9.1).

    The availability gate is the browser-presence check; a False answer is
    exactly the ``needs_browser`` prompt the frontend offers the install from.
    """
    edge = _Edge(installed=False)
    store, _factory = _store(edge, monkeypatch=monkeypatch)

    assert await store.is_available() is False
    # No install was attempted by merely checking availability.
    edge.install.assert_not_awaited()


async def test_installed_reports_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An installed browser → ``is_available`` is True (prerequisite met)."""
    edge = _Edge(installed=True)
    store, _factory = _store(edge, monkeypatch=monkeypatch)
    # Connected = browser installed AND a usable Luna session (the _store
    # helper stubs a usable session by default).
    assert await store.is_available() is True
    # Browser installed but NO usable session → not connected (the bug fix:
    # Luna must not report connected on a bare browser install).
    store._session = SimpleNamespace(is_usable=lambda: False)
    assert await store.is_available() is False


# ── flatpak absent → flatpak_not_found (Req 9.6) ──────────────────────


async def test_flatpak_absent_reports_flatpak_not_found_and_attempts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flatpak missing → ``flatpak_not_found``; nothing attempted (Req 9.6).

    The installer is never called, no overrides are applied, and the resume
    is not run — the pre-install state is kept untouched.
    """
    edge = _Edge(installed=False)
    store, factory = _store(edge, monkeypatch=monkeypatch, has_flatpak=False)
    resume = AsyncMock()

    result = await store.install_browser(resume=resume)

    assert isinstance(result, Result)
    assert result.success is False
    assert result.store == LUNA_STORE
    assert result.error == FLATPAK_NOT_FOUND
    assert result.error_code == FLATPAK_NOT_FOUND
    # Nothing was attempted: no install, no overrides, no resume.
    edge.install.assert_not_awaited()
    assert factory.instances == []
    resume.assert_not_awaited()


# ── install failure / >600 s → installation_failed (Req 9.4, 9.5) ─────


async def test_install_failure_reports_installation_failed_with_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed install → ``installation_failed`` carrying the reason (Req 9.5).

    The installer's own error string is surfaced in ``metadata["reason"]`` and
    the interrupted operation is left in its pre-install state (not resumed),
    with no overrides applied.
    """
    edge = _Edge(
        installed=False,
        install_result={"success": False, "error": "microsoft.browserInstallFailed"},
    )
    store, factory = _store(edge, monkeypatch=monkeypatch)
    resume = AsyncMock()

    result = await store.install_browser(resume=resume)

    assert result.success is False
    assert result.error == INSTALLATION_FAILED
    assert result.error_code == INSTALLATION_FAILED
    assert result.metadata["reason"] == "microsoft.browserInstallFailed"
    # The install was attempted exactly once; pre-install state kept.
    edge.install.assert_awaited_once()
    assert factory.instances == []  # no overrides on the failure path
    resume.assert_not_awaited()


async def test_install_timeout_reports_installation_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An install exceeding 600 s → ``installation_failed`` (Req 9.4/9.5).

    ``_run_browser_install`` wraps ``EdgeBrowser.install`` in a 600 s timeout;
    a ``TimeoutError`` from that wait is mapped to ``installation_failed`` with
    a reason naming the budget, and the interrupted operation is not resumed.
    """
    edge = _Edge(installed=False, install_exc=TimeoutError())
    store, factory = _store(edge, monkeypatch=monkeypatch)
    resume = AsyncMock()

    result = await store.install_browser(resume=resume)

    assert result.success is False
    assert result.error == INSTALLATION_FAILED
    assert str(int(BROWSER_INSTALL_BUDGET_SECONDS)) in result.metadata["reason"]
    assert factory.instances == []
    resume.assert_not_awaited()


# ── success within 600 s → overrides + resume (Req 9.2–9.4) ───────────


async def test_success_applies_overrides_then_resumes_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful install applies overrides then resumes the op (Req 9.2–9.4).

    The Luna profile-dir + ``/run/udev:ro`` overrides are applied (built from
    the configured ``profile_dir``), then the interrupted operation runs and
    its result/status is folded back: a resumed ``start_auth`` returning a
    connected :class:`AuthResult` leaves the prerequisite result successful and
    carrying that ``store_status``, with ``metadata["resumed"]`` set.
    """
    edge = _Edge(installed=False, install_result={"success": True})
    store, factory = _store(edge, monkeypatch=monkeypatch)

    resumed_auth = AuthResult(
        success=True, store=LUNA_STORE, tokens_cached=True,
        metadata={STATUS_KEY: LunaStatus.CONNECTED},
    )
    resume = AsyncMock(return_value=resumed_auth)

    result = await store.install_browser(resume=resume)

    # Install ran, override applied once, resume ran.
    edge.install.assert_awaited_once()
    assert len(factory.instances) == 1
    override = factory.instances[0]
    assert override.profile_dir == _PROFILE_DIR
    assert override.apply_calls == 1
    resume.assert_awaited_once()

    # The resumed operation's own result is surfaced, flagged as resumed.
    assert result.success is True
    assert result.store == LUNA_STORE
    assert result.metadata["resumed"] is True
    assert result.metadata["overrides_applied"] is True
    assert result.metadata[STATUS_KEY] == LunaStatus.CONNECTED


async def test_success_resumes_get_library_folding_list_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed ``get_library`` folds its list/None into the result (Req 9.2/9.3).

    ``get_library`` returns a ``list[Game] | None`` rather than a ``Result``: a
    non-None list (the catalog was read) folds to success=True; the resume
    runs only after the overrides are applied.
    """
    edge = _Edge(installed=False, install_result={"success": True})
    store, factory = _store(edge, monkeypatch=monkeypatch)

    resume = AsyncMock(return_value=[])  # empty but authoritative library

    result = await store.install_browser(resume=resume)

    assert len(factory.instances) == 1
    assert factory.instances[0].apply_calls == 1
    resume.assert_awaited_once()
    assert result.success is True  # a list (even empty) means usable
    assert result.metadata["resumed"] is True


async def test_success_resumed_get_library_none_is_unsuccessful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed ``get_library`` returning None folds to an unsuccessful result.

    The overrides are still applied (the install succeeded), but ``None`` means
    the catalog still could not be read, so the folded result is not successful.
    """
    edge = _Edge(installed=False, install_result={"success": True})
    store, factory = _store(edge, monkeypatch=monkeypatch)

    resume = AsyncMock(return_value=None)

    result = await store.install_browser(resume=resume)

    assert factory.instances[0].apply_calls == 1
    assert result.success is False
    assert result.metadata["resumed"] is True


async def test_success_without_resume_returns_bare_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful install with no resume applies overrides and reports success.

    When no interrupted operation is handed in, the overrides are still applied
    and a bare success is returned with ``resumed=False`` so a caller that only
    needs the browser present can use the install standalone.
    """
    edge = _Edge(installed=False, install_result={"success": True})
    store, factory = _store(edge, monkeypatch=monkeypatch)

    result = await store.install_browser(resume=None)

    assert factory.instances[0].apply_calls == 1
    assert result.success is True
    assert result.metadata["resumed"] is False
    assert result.metadata["overrides_applied"] is True


async def test_already_installed_skips_install_and_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-installed browser skips the install and resumes directly.

    A racing second caller still gets its interrupted operation finished: the
    installer is never invoked, but the overrides are applied and the resume
    runs so the blocked operation completes.
    """
    edge = _Edge(installed=True)
    store, factory = _store(edge, monkeypatch=monkeypatch)
    resume = AsyncMock(return_value=[])

    result = await store.install_browser(resume=resume)

    edge.install.assert_not_awaited()
    assert factory.instances[0].apply_calls == 1
    resume.assert_awaited_once()
    assert result.success is True
    assert result.metadata["resumed"] is True
