"""Public ``GOGInstaller`` — orchestrates the full install flow.

OP-22-gog-install-installer
File: py_modules/unifideck/stores/gog/install/installer.py

Top-level façade composing the focused
collaborators (``_InstallHelpers``,
``_PostInstallMarker``, ``_GogdlProgressMonitor``,
``_UninstallPipeline``, ``GOGInstallPlanner``).
Each phase of the install lives in its own
method so the flow is auditable.

Install pipeline (in order):

1. **Preflight** — validate gogdl exists, resolve
   the install base path, build the ``_InstallContext``;
2. **Probe + prepare** — load + refresh tokens,
   wipe stale caches, probe game info (platform +
   folder + langs), pick install mode (download
   vs repair);
3. **gogdl phase** — actually run the
   install/repair subprocess with progress
   reporting + post-install verify pass;
4. **Finalize** — locate the install on disk,
   write the marker file, verify completeness,
   regenerate the manifest.

Failure at any stage returns an ``InstallResult``
with a specific error code; the
``_install_failed`` helper optionally cleans up
partial install state before returning.

The ``_InstallContext`` dataclass carries the
mutable per-install state between phases so no
phase needs to recompute things like the platform
or supported languages.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from ....core.types import InstallResult, Result
from ..config import GOGConfig
from ..tokens import GOGTokenManager
from .helpers import _InstallHelpers
from .marker import _PostInstallMarker
from .planner import GOGInstallPlanner
from .progress import _GogdlProgressMonitor
from .uninstall_pipeline import _UninstallPipeline

logger = logging.getLogger(__name__)


@dataclass
class _InstallContext:
    """Per-install mutable state — populated incrementally across phases.

    Some fields (``game_id``, ``base_path``,
    ``preferred_lang``, ``explicit_lang``,
    ``progress_cb``) are set at preflight; the
    rest are filled in by later phases.

    Using a dataclass over passing many args
    between phase methods avoids 7+ arg signatures
    and keeps the flow readable.
    """

    game_id: str
    base_path: str
    preferred_lang: str
    explicit_lang: bool
    progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None
    platform: str = ""
    folder_name: str | None = None
    supported_langs: list[str] = field(default_factory=list)
    existing_dirs: set = field(default_factory=set)
    support_dir: str = ""
    install_mode: str = ""
    found_path: str = ""


class GOGInstaller:
    """Public installer — single entry point for install/uninstall.

    Composes the focused collaborators
    (planner/helpers/marker/progress/uninstall)
    behind a unified API. Dependencies are
    injected at construction; everything else is
    state held by the collaborators.

    The ``_find_exe`` callable is provided by the
    parent ``GOGStore`` — kept as a callable
    rather than a direct reference to
    ``GOGExeResolver`` so tests can swap in fakes
    without instantiating the full resolver.
    """

    def __init__(
        self,
        config: GOGConfig,
        tokens: GOGTokenManager,
        gogdl_bin: str,
        exe_finder: Callable[[str], str | None],
        locale_fn: Callable[[], str]
    ) -> None:
        """Wire all collaborators with the shared config + tokens.

        Args:
            config: ``GOGConfig``.
            tokens: ``GOGTokenManager``.
            gogdl_bin: absolute path to the gogdl
                binary.
            exe_finder: callable that locates the
                game exe in an install dir.
            locale_fn: callable returning the
                user's current locale (re-read on
                each install so language changes
                take effect).
        """
        self._config = config
        self._tokens = tokens
        self._gogdl_bin = gogdl_bin
        self._find_exe = exe_finder
        self._locale_fn = locale_fn
        self._planner = GOGInstallPlanner(config, tokens)
        self._planner.set_gogdl_bin(gogdl_bin)
        self._uninstall_pipeline = _UninstallPipeline(self)
        self._progress_monitor = _GogdlProgressMonitor(self)
        self._marker = _PostInstallMarker(self)
        self._helpers = _InstallHelpers(self)

    async def uninstall_game(self, game_id: str, install_path: str | None = None) -> Result:
        """Delegate to ``_UninstallPipeline.uninstall_game``.

        Args:
            game_id: product id.
            install_path: dir to remove.

        Returns:
            ``Result``.
        """
        return await self._uninstall_pipeline.uninstall_game(game_id, install_path)

    async def _run_gogdl_with_progress(
        self,
        install_mode: str,
        game_id: str,
        platform: str,
        path: str,
        support_dir: str,
        languages: list[str],
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None
    ) -> bool:
        """Proxy to ``_GogdlProgressMonitor.run_gogdl_with_progress``.

        Args:
            install_mode: ``"install"`` or
                ``"update"``.
            game_id: product id.
            platform: target.
            path: install path.
            support_dir: gogdl support cache.
            languages: codes.
            progress_cb: optional callback.

        Returns:
            True on success.
        """
        return await self._progress_monitor.run_gogdl_with_progress(install_mode, game_id, platform, path, support_dir, languages, progress_cb)

    async def _run_gogdl_repair_pass(self, game_id: str, platform: str, base_path: str, folder_name: str | None, preferred_lang: str) -> None:
        """Proxy to ``_GogdlProgressMonitor.run_gogdl_repair_pass``.

        Args:
            game_id: product id.
            platform: target.
            base_path: install root.
            folder_name: predicted folder.
            preferred_lang: lang to verify.
        """
        await self._progress_monitor.run_gogdl_repair_pass(game_id, platform, base_path, folder_name, preferred_lang)

    def _snapshot_dirs(self, base_path: str) -> set:
        """Proxy to ``_PostInstallMarker.snapshot_dirs``.

        Args:
            base_path: dir to snapshot.

        Returns:
            Set of names.
        """
        return self._marker.snapshot_dirs(base_path)

    async def _locate_install(self, game_id: str, base_path: str, folder_name: str | None, existing_dirs: set) -> str | None:
        """Proxy to ``_PostInstallMarker.locate_install``.

        Args:
            game_id: product id.
            base_path: install root.
            folder_name: predicted name.
            existing_dirs: pre-install snapshot.

        Returns:
            Located path or ``None``.
        """
        return await self._marker.locate_install(game_id, base_path, folder_name, xisting_dirs)

    async def _write_install_marker(self, install_path: str, game_id: str, language: str) -> bool:
        """Proxy to ``_PostInstallMarker.write_install_marker``.

        Args:
            install_path: install root.
            game_id: product id.
            language: chosen lang.

        Returns:
            True on success.
        """
        return await self._marker.write_install_marker(install_path, game_id, language)

    async def _regenerate_manifest(self, game_id: str, platform: str, ) -> None:
        """Proxy to ``_PostInstallMarker.regenerate_manifest``.

        Args:
            game_id: product id.
            platform: target.
        """
        await self._marker.regenerate_manifest(game_id, platform)

    def _install_failed(
        self,
        game_id: str,
        error: str,
        *,
        cleanup_path: str | None = None,
        cleanup_folder: str | None = None
    ) -> InstallResult:
        """Build a failure ``InstallResult``, optionally cleaning partial install.

        ``cleanup_path`` + ``cleanup_folder``
        kw-args trigger ``_cleanup_partial`` to
        remove the predicted folder if it
        exists. Kept kw-only because most
        failure cases don't need cleanup
        (preflight failures, token failures).

        Args:
            game_id: product id.
            error: short error code.
            cleanup_path: install base.
            cleanup_folder: folder to remove
                under base.

        Returns:
            Failure ``InstallResult``.
        """
        if cleanup_path is not None:
            self._cleanup_partial(cleanup_path, cleanup_folder)
        return InstallResult(
            success=False,
            error=error,
            store="gog",
            game_id=game_id,
        )

    async def install_game(
        self,
        game_id: str,
        base_path: str | None = None,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        language: str | None = None
    ) -> InstallResult:
        """Top-level install entry point — drives the four-phase pipeline.

        Each phase can short-circuit with an
        ``InstallResult``; on success returns
        the final result from ``_install_finalize``.

        The ``cast`` on ``failure`` is needed for
        mypy — the preflight returns a tuple
        ``(ctx, failure)`` where ``failure`` is
        either ``None`` or an ``InstallResult``,
        and the type narrowing isn't picked up
        automatically.

        Args:
            game_id: product id.
            base_path: install root (default:
                ``config.download_dir``).
            progress_cb: optional callback.
            language: explicit language code
                (default: locale-based pick).

        Returns:
            ``InstallResult``.
        """
        ctx, failure = self._install_preflight(game_id, base_path, progress_cb, language)
        if failure is not None:
            return cast("InstallResult", failure)
        auth_failure = await self._install_probe_and_prepare(ctx)
        if auth_failure is not None:
            return auth_failure
        download_failure = await self._install_run_gogdl_phase(ctx)
        if download_failure is not None:
            return download_failure
        return await self._install_finalize(ctx)

    def _install_preflight(
        self,
        game_id: str,
        base_path: str | None,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None,
        language: str | None
    ) -> tuple:
        """Synchronous preflight — validate gogdl bin, resolve paths, build context.

        Three steps:

        1. Verify the gogdl binary exists on
           disk; missing → ``gogdl_not_found``;
        2. Resolve the base path (caller-supplied
           or ``config.download_dir``), mkdir if
           needed;
        3. Resolve the preferred language (caller
           explicit value or locale fallback to
           en-US);
        4. Build the ``_InstallContext``.

        Args:
            game_id: product id.
            base_path: optional override.
            progress_cb: optional callback.
            language: optional explicit lang.

        Returns:
            ``(ctx, None)`` on success or
            ``(None, failure_result)``.
        """
        if not os.path.isfile(self._gogdl_bin):
            return None, self._install_failed(
                game_id,
                "gogdl_not_found",
            )
        resolved_base = base_path or os.path.expanduser(
            self._config.download_dir,
        )
        os.makedirs(resolved_base, exist_ok=True)
        preferred_lang = language or self._locale_fn() or "en-US"
        explicit_lang = language is not None
        logger.info(
            "[GOGInstaller] start: game=%s path=%s lang=%s (explicit=%s)",
            game_id,
            resolved_base,
            preferred_lang,
            explicit_lang,
        )
        ctx = _InstallContext(
            game_id=game_id,
            base_path=resolved_base,
            preferred_lang=preferred_lang,
            explicit_lang=explicit_lang,
            progress_cb=progress_cb,
        )
        return ctx, None

    async def _install_probe_and_prepare(self, ctx: _InstallContext) -> InstallResult | None:
        """Phase 2 — tokens, cache wipe, game probe, install-mode pick.

        Pipeline:

        1. Load tokens if not already loaded;
        2. Refresh if stale → failure →
           ``not_authenticated``;
        3. Wipe stale manifests + support cache
           (pre-install cleanliness);
        4. Probe game info (platform + folder +
           langs);
        5. Snapshot the base dir for post-install
           diff;
        6. Create the support dir;
        7. Decide install mode (download vs
           repair);
        8. Wipe manifests again (gogdl info from
           probe may have cached something stale).

        Args:
            ctx: shared context (mutated in
                place).

        Returns:
            ``None`` on success, or failure
            ``InstallResult``.
        """
        if not self._tokens.has_tokens:
            await self._tokens.load()
        if not await self._tokens.refresh_if_stale():
            return self._install_failed(
                ctx.game_id,
                "not_authenticated",
            )
        await self._wipe_manifests(ctx.game_id)
        await self._wipe_support_cache(ctx.game_id)
        (
            ctx.platform,
            ctx.folder_name,
            ctx.supported_langs,
        ) = await self._helpers.probe_game_info(ctx.game_id)
        ctx.existing_dirs = self._snapshot_dirs(ctx.base_path)
        ctx.support_dir = os.path.join(
            os.path.expanduser(self._config.gogdl_config_dir),
            "gog-support",
            ctx.game_id,
        )
        os.makedirs(ctx.support_dir, exist_ok=True)
        target_folder = (
            os.path.join(ctx.base_path, ctx.folder_name) if ctx.folder_name else None
        )
        ctx.install_mode = await self._planner.determine_install_mode(
            ctx.game_id,
            target_folder,
        )
        await self._wipe_manifests(ctx.game_id)
        return None

    async def _install_run_gogdl_phase(self, ctx: _InstallContext) -> InstallResult | None:
        """Phase 3 — actually run gogdl install/update with progress.

        Pipeline:

        1. Compute the gogdl ``--path`` arg —
           depends on install mode (download
           uses base_path, repair uses
           base_path/folder_name);
        2. Pick languages via helpers;
        3. Run gogdl with progress reporting;
        4. Failed → ``download_failed`` with
           partial cleanup;
        5. Success → run repair pass (non-fatal
           verification).

        Args:
            ctx: shared context.

        Returns:
            ``None`` on success or failure
            ``InstallResult``.
        """
        gogdl_path = (
            ctx.base_path
            if ctx.install_mode == "download"
            else (
                os.path.join(ctx.base_path, ctx.folder_name)
                if ctx.folder_name
                else ctx.base_path
            )
        )
        languages = self._helpers.pick_languages(
            ctx.preferred_lang,
            ctx.explicit_lang,
            ctx.supported_langs,
        )
        download_ok = await self._run_gogdl_with_progress(
            install_mode=ctx.install_mode,
            game_id=ctx.game_id,
            platform=ctx.platform,
            path=gogdl_path,
            support_dir=ctx.support_dir,
            languages=languages,
            progress_cb=ctx.progress_cb,
        )
        if not download_ok:
            return self._install_failed(
                ctx.game_id,
                "download_failed",
                cleanup_path=ctx.base_path,
                cleanup_folder=ctx.folder_name,
            )
        await self._run_gogdl_repair_pass(
            ctx.game_id,
            ctx.platform,
            ctx.base_path,
            ctx.folder_name,
            ctx.preferred_lang,
        )
        return None

    async def _install_finalize(self, ctx: _InstallContext) -> InstallResult:
        """Phase 4 — locate, mark, verify, regenerate manifest.

        Pipeline:

        1. Locate the install (3-stage:
           predicted folder / flat / scan);
        2. Not found → ``install_not_located``;
        3. Write the marker file → fail →
           ``marker_write_failed``;
        4. Run verification (size, info, exe);
           on issues, log WARN but still
           return success — the install is
           usable even if not perfect;
        5. Regenerate the manifest cache;
        6. Return success with ``found_path``.

        Args:
            ctx: shared context.

        Returns:
            ``InstallResult``.
        """
        found_path = await self._locate_install(
            ctx.game_id,
            ctx.base_path,
            ctx.folder_name,
            ctx.existing_dirs,
        )
        if not found_path:
            return self._install_failed(
                ctx.game_id,
                "install_not_located",
                cleanup_path=ctx.base_path,
                cleanup_folder=ctx.folder_name,
            )
        marker_ok = await self._write_install_marker(
            found_path,
            ctx.game_id,
            ctx.preferred_lang,
        )
        if not marker_ok:
            return self._install_failed(
                ctx.game_id,
                "marker_write_failed",
                cleanup_path=ctx.base_path,
                cleanup_folder=ctx.folder_name,
            )
        verification = await self._planner.verify_installation(
            ctx.game_id,
            found_path,
            ctx.platform,
            self._find_exe,
        )
        if not verification.get("complete"):
            logger.warning(
                "[GOGInstaller] verification issue: %s",
                verification.get("issue", "unknown"),
            )
        await self._regenerate_manifest(
            ctx.game_id,
            ctx.platform,
        )
        logger.info(
            "[GOGInstaller] install complete: %s",
            found_path,
        )
        return InstallResult(
            success=True,
            store="gog",
            game_id=ctx.game_id,
            install_path=found_path,
        )

    async def _wipe_manifests(self, game_id: str) -> None:
        """Remove all known manifest cache locations for ``game_id``.

        Same four locations as
        ``GOGInstallPlanner._manifest_locations``
        — kept in sync with that method's list.
        Per-file errors are logged but don't
        propagate.

        Args:
            game_id: product id.
        """

        def _sync() -> None:
            """Blocking manifest cleanup — iterate locations + unlink missing-OK.

            Runs in worker thread via
            ``asyncio.to_thread``.
            """
            base = os.path.expanduser(
                self._config.gogdl_config_dir,
            )
            parent = os.path.dirname(base)
            locations = [
                os.path.join(
                    base,
                    "heroic_gogdl",
                    "manifests",
                    game_id,
                ),
                os.path.join(
                    parent,
                    "heroic_gogdl",
                    "manifests",
                    game_id,
                ),
                os.path.join(base, "manifests", game_id),
                os.path.join(
                    parent,
                    "gogdl",
                    "manifests",
                    game_id,
                ),
            ]
            for path in locations:
                if os.path.isfile(path):
                    try:
                        os.remove(path)
                        logger.info(
                            "[GOGInstaller] cleared manifest: %s",
                            path,
                        )
                    except OSError as e:
                        logger.warning(
                            "[GOGInstaller] could not clear manifest: %s",
                            e,
                        )

        await asyncio.to_thread(_sync)

    async def _wipe_support_cache(self, game_id: str) -> None:
        """Remove the per-game support cache directory.

        Support cache is gogdl's per-product
        scratch space (downloaded chunks, partial
        files). Wiping it ensures a clean install
        — leftover state can confuse gogdl into
        thinking files are already on disk.

        Args:
            game_id: product id.
        """

        def _sync() -> None:
            """Blocking rmtree of the per-game support cache, logged.

            Runs in a worker thread; per-error
            warnings, no propagation.
            """
            support_dir = os.path.join(
                os.path.expanduser(
                    self._config.gogdl_config_dir,
                ),
                "gog-support",
                game_id,
            )
            if os.path.isdir(support_dir):
                try:
                    shutil.rmtree(support_dir)
                    logger.info(
                        "[GOGInstaller] cleared support cache",
                    )
                except OSError as e:
                    logger.warning(
                        "[GOGInstaller] support cleanup: %s",
                        e,
                    )

        await asyncio.to_thread(_sync)

    def _cleanup_partial(self, base_path: str, folder_name: str | None) -> None:
        """Remove a partial install dir after a failed phase.

        Called by ``_install_failed`` when
        ``cleanup_path`` is set. Uses
        ``shutil.rmtree(ignore_errors=True)`` so
        a failure here never stacks on top of the
        failure that triggered cleanup.

        No folder_name → no-op (we don't know
        what to remove).

        Args:
            base_path: install root.
            folder_name: subdir under base.
        """
        if not folder_name:
            return
        partial = os.path.join(base_path, folder_name)
        if os.path.exists(partial):
            logger.info(
                "[GOGInstaller] cleanup partial: %s",
                partial,
            )
            shutil.rmtree(partial, ignore_errors=True)
