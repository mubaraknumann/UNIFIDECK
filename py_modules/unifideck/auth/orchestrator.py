"""Auth-flow orchestrator — drives get-URL → wait-redirect → exchange-code.

OP-15b | py_modules/unifideck/auth/orchestrator.py

Top-level coordinator for an OAuth auth flow. Per
attempt:

1. Emit ``STORE_AUTH_STARTED``;
2. Call ``get_url()`` (store-specific URL builder);
3. Optionally write the URL to a file (frontend
   reads it to drive the browser);
4. Either:
   * **Foreground mode** — await the redirect + run
     ``exchange_code`` inline, return the ``AuthResult``;
   * **Background mode** — spawn an asyncio task,
     return immediately with ``metadata={"pending": True}``
     so the caller doesn't block; the task drives the
     same pipeline and emits the final events
     itself.

Every failure point emits ``STORE_AUTH_FAILED`` with
a typed error code (``get_url_failed`` / ``no_url`` /
``url_write_failed`` / ``capture_failed`` / ``no_code``
/ ``exchange_failed`` / ``monitor_crashed``).

The background task is tracked via ``_bg_task`` —
``cancel_background`` cancels an in-flight flow (used
when the user closes the auth modal).

The ``write_url_file`` mechanism uses a temp+rename
atomic write so the frontend never reads a half-written
URL.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.types import AuthResult, Events

if TYPE_CHECKING:
    from ..event_bus.event_bus import EventBus
    from .browser import OAuthBrowserMonitor

    GetUrlCallback = Callable[[], Awaitable[str]]
    ExchangeCodeCallback = Callable[[str], Awaitable[AuthResult]]

logger = logging.getLogger(__name__)


@dataclass
class OrchestratorConfig:
    """Tunable parameters for the auth flow.

    Attributes:
        timeout: total wait-for-redirect timeout in
            seconds (default 300 = 5 minutes).
        browser_launch_grace: pause between
            URL-write and starting the poll loop;
            gives the browser time to actually open
            the tab before we start hunting for it.
    """

    timeout: float = 300.0
    browser_launch_grace: float = 1.5


class AuthOrchestrator:
    """Per-store auth flow coordinator (foreground + background modes)."""

    def __init__(
        self,
        bus: EventBus,
        browser_monitor: OAuthBrowserMonitor,
        store_name: str,
        config: OrchestratorConfig | None = None,
    ) -> None:
        """Bind dependencies and prepare the background-task slot.

        Args:
            bus: event bus (emits STORE_AUTH_*
                lifecycle events).
            browser_monitor: redirect monitor.
            store_name: identifier used in events +
                log lines.
            config: optional tuning; defaults to
                ``OrchestratorConfig()``.
        """
        self._bus = bus
        self._monitor = browser_monitor
        self._store = store_name
        self._cfg = config or OrchestratorConfig()
        self._bg_task: asyncio.Task | None = None

    async def run_flow(
        self,
        get_url: GetUrlCallback,
        allowed_uris: list[str],
        exchange_code: ExchangeCodeCallback,
        *,
        timeout: float | None = None,
        write_url_file: str | None = None,
        background: bool = False,
    ) -> AuthResult:
        """Orchestrate one auth flow (foreground or background).

        Pipeline:

        1. Emit ``STORE_AUTH_STARTED``;
        2. ``get_url()`` → emit ``get_url_failed`` on
           exception or ``no_url`` on empty result;
        3. Optionally atomic-write URL to a file →
           emit ``url_write_failed`` on failure;
        4. Dispatch to foreground or background
           path.

        Args:
            get_url: coroutine returning the auth URL.
            allowed_uris: prefix list passed to the
                monitor.
            exchange_code: coroutine that swaps the
                OAuth code for tokens.
            timeout: override the config timeout.
            write_url_file: optional path to persist
                the URL for the frontend.
            background: if True, return immediately
                after spawning the task.

        Returns:
            ``AuthResult`` — foreground: full
            outcome; background: ``success=True``
            with ``metadata={"pending": True}``.
        """
        deadline = timeout if timeout is not None else self._cfg.timeout
        await self._emit_started()
        try:
            url = await get_url()
        except Exception as e:
            logger.error(
                "[AuthOrchestrator/%s] get_url failed: %s",
                self._store,
                e,
            )
            return await self._emit_failed(
                "get_url_failed",
                str(e),
            )
        if not url:
            return await self._emit_failed(
                "no_url",
                "get_url returned empty string",
            )
        if write_url_file:
            write_ok = await self._write_url_atomically(
                write_url_file,
                url,
            )
            if not write_ok:
                return await self._emit_failed(
                    "url_write_failed",
                    f"could not write URL to {write_url_file}",
                    url=url,
                )
        if background:
            return self._spawn_background_task(
                url=url,
                allowed_uris=allowed_uris,
                exchange_code=exchange_code,
                deadline=deadline,
            )
        return await self._await_redirect_and_exchange(
            url=url,
            allowed_uris=allowed_uris,
            exchange_code=exchange_code,
            deadline=deadline,
        )

    def cancel_background(self) -> bool:
        """Cancel the in-flight background task if any.

        Idempotent: returns False when no task is
        running or the task already finished. The
        cancelled task's CancelledError is swallowed
        inside the runner (see
        ``_spawn_background_task``).

        Returns:
            True if a task was cancelled.
        """
        task = self._bg_task
        if task is None or task.done():
            return False
        task.cancel()
        self._bg_task = None
        return True

    async def _await_redirect_and_exchange(
        self,
        url: str,
        allowed_uris: list[str],
        exchange_code: ExchangeCodeCallback,
        deadline: float,
    ) -> AuthResult:
        """Wait for the OAuth redirect, then exchange the code for tokens.

        Five-step:

        1. Sleep ``browser_launch_grace`` to let the
           browser open the tab;
        2. Call the monitor's ``wait_for_redirect``;
        3. On capture failure → emit ``capture_failed``;
        4. On missing ``code`` param → emit
           ``no_code``;
        5. Close the tab + run the code-for-token
           exchange.

        Cancellation propagates through (re-raise of
        ``CancelledError``) so the background runner
        can react.

        Args:
            url: auth URL (logged for context).
            allowed_uris: prefix list.
            exchange_code: token-exchange callback.
            deadline: redirect timeout.

        Returns:
            ``AuthResult``.
        """
        logger.info(
            "[AuthOrchestrator/%s] waiting for redirect to %s (timeout=%.0fs)",
            self._store,
            allowed_uris,
            deadline,
        )
        await asyncio.sleep(self._cfg.browser_launch_grace)
        try:
            capture = await self._monitor.wait_for_redirect(
                allowed_uris=allowed_uris,
                timeout=deadline,
            )
        except asyncio.CancelledError:
            logger.info(
                "[AuthOrchestrator/%s] flow cancelled",
                self._store,
            )
            raise
        except Exception as e:
            logger.error(
                "[AuthOrchestrator/%s] monitor crashed: %s",
                self._store,
                e,
            )
            return await self._emit_failed("monitor_crashed", str(e))
        if not capture.success:
            return await self._emit_failed(
                capture.error or "capture_failed",
                f"browser capture failed after {capture.elapsed_seconds:.1f}s",
                url=url,
            )
        code = capture.code
        if not code:
            return await self._emit_failed(
                "no_code",
                f"redirect to {capture.redirect_url} carried no `code` parameter",
                url=url,
            )
        await self._close_tab_safely(capture.redirect_url)
        return await self._finalize_auth_exchange(
            code,
            url,
            exchange_code,
        )

    async def _finalize_auth_exchange(
        self,
        code: str,
        url: str,
        exchange_code: ExchangeCodeCallback,
    ) -> AuthResult:
        """Run ``exchange_code`` and emit the terminal STORE_AUTH event.

        Three-arm outcome handling:

        * Exception during exchange → emit
          ``exchange_failed`` + return the failed
          result.
        * Success → emit ``STORE_AUTH_COMPLETE`` +
          INFO log.
        * Result with ``success=False`` → emit
          ``STORE_AUTH_FAILED`` with the result's
          error code (or ``"exchange_returned_failure"``
          if none).

        ``result.store`` is set unconditionally so
        downstream consumers don't need to track it.

        Args:
            code: OAuth code from the redirect.
            url: original auth URL (for failure
                metadata).
            exchange_code: token-exchange callback.

        Returns:
            ``AuthResult``.
        """
        try:
            result = await exchange_code(code)
        except Exception as e:
            logger.error(
                "[AuthOrchestrator/%s] exchange_code failed: %s",
                self._store,
                e,
            )
            return await self._emit_failed(
                "exchange_failed",
                str(e),
                url=url,
            )
        result.store = self._store
        if result.success:
            await self._bus.emit(
                Events.STORE_AUTH_COMPLETE,
                store=self._store,
            )
            logger.info(
                "[AuthOrchestrator/%s] auth complete",
                self._store,
            )
        else:
            await self._bus.emit(
                Events.STORE_AUTH_FAILED,
                store=self._store,
                error=result.error or "exchange_returned_failure",
            )
            logger.warning(
                "[AuthOrchestrator/%s] exchange failed: %s",
                self._store,
                result.error,
            )
        return result

    def _spawn_background_task(
        self,
        url: str,
        allowed_uris: list[str],
        exchange_code: ExchangeCodeCallback,
        deadline: float,
    ) -> AuthResult:
        """Start an asyncio task running the redirect-exchange flow.

        Cancels any pre-existing background task
        first (only one flow active at a time per
        store). The runner coroutine catches its own
        CancelledError so a forced cancel doesn't
        propagate.

        The slot ``_bg_task`` is cleared in the
        ``finally`` once the task finishes (the
        ``and done()`` guard handles the rare race
        where two tasks run briefly).

        Args:
            url: auth URL.
            allowed_uris: prefix list.
            exchange_code: token-exchange callback.
            deadline: redirect timeout.

        Returns:
            ``AuthResult`` with ``pending=True``
            metadata.
        """
        self.cancel_background()

        async def _background_runner() -> None:
            """Run the redirect-exchange pipeline in the background task.

            Swallows ``CancelledError`` so an explicit
            cancel from ``cancel_background`` exits
            cleanly. The ``finally`` clears
            ``_bg_task`` so subsequent flows can run.
            """
            try:
                await self._await_redirect_and_exchange(
                    url=url,
                    allowed_uris=allowed_uris,
                    exchange_code=exchange_code,
                    deadline=deadline,
                )
            except asyncio.CancelledError:
                pass
            finally:
                if self._bg_task is not None and self._bg_task.done():
                    self._bg_task = None

        self._bg_task = asyncio.create_task(
            _background_runner(),
            name=f"auth_flow_{self._store}",
        )
        logger.info(
            "[AuthOrchestrator/%s] background flow started",
            self._store,
        )
        return AuthResult(
            success=True,
            store=self._store,
            url=url,
            metadata={"pending": True},
        )

    async def _emit_started(self) -> None:
        """Fire ``STORE_AUTH_STARTED`` with the bound store identifier.

        One-liner — the lifecycle event with just the
        store id.
        """
        await self._bus.emit(Events.STORE_AUTH_STARTED, store=self._store)

    async def _emit_failed(
        self,
        error_code: str,
        detail: str,
        url: str | None = None,
    ) -> AuthResult:
        """Log + emit ``STORE_AUTH_FAILED`` + return a failed ``AuthResult``.

        Three-in-one: callers can write
        ``return await self._emit_failed(...)`` to
        produce the WARN log, fire the event, and
        return the typed failure result in one
        statement.

        Args:
            error_code: machine-readable failure code.
            detail: human-readable detail (logged,
                not embedded in the event).
            url: optional original auth URL (kept on
                the result for diagnostics).

        Returns:
            Failed ``AuthResult``.
        """
        logger.warning(
            "[AuthOrchestrator/%s] %s: %s",
            self._store,
            error_code,
            detail,
        )
        await self._bus.emit(
            Events.STORE_AUTH_FAILED,
            store=self._store,
            error=error_code,
        )
        return AuthResult(
            success=False,
            error=error_code,
            store=self._store,
            url=url,
        )

    async def _close_tab_safely(self, url_substring: str | None) -> None:
        """Extract domain from the redirect URL and ask the monitor to close.

        URL → domain extraction: drop the scheme,
        drop everything from the first slash. The
        domain is what the CDP target list shows in
        its URL field.

        All failures are swallowed at DEBUG — closing
        the tab is best-effort cleanup, not a
        correctness requirement.

        Args:
            url_substring: redirect URL (None →
                no-op).
        """
        try:
            if url_substring is None:
                return
            domain = url_substring
            if "://" in domain:
                domain = domain.split("://", 1)[1]
            if "/" in domain:
                domain = domain.split("/", 1)[0]
            await self._monitor.close_oauth_tab(domain)
        except Exception as e:
            logger.debug(
                "[AuthOrchestrator/%s] close_oauth_tab failed (ignored): %s",
                self._store,
                e,
            )

    @staticmethod
    async def _write_url_atomically(path: str, url: str) -> bool:
        """Write ``url`` to ``path`` atomically; runs the I/O in a thread.

        Uses ``asyncio.to_thread`` because the sync
        I/O (mkdir + write + replace) shouldn't block
        the event loop on slow storage.

        Inside the thread:

        * Expand ``~`` in path;
        * mkdir the parent;
        * Write to ``<path>.tmp``;
        * ``replace`` over the target (atomic on
          POSIX).

        OSError logs at ERROR + returns False. The
        outer ``expanded`` reference in the error
        block is set inside the sync function — if
        the OSError fires before assignment, the
        binding might be unset; the except is mostly
        defensive against rare cases.

        Args:
            path: target file path.
            url: URL string to write.

        Returns:
            True on success.
        """

        def _write_sync() -> str:
            """Synchronous body — does the mkdir + write + replace.

            Returns:
                The expanded target path (for
                logging).
            """
            expanded = Path(path).expanduser()
            parent = expanded.parent
            parent.mkdir(parents=True, exist_ok=True)
            tmp = expanded.with_name(expanded.name + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                f.write(url)
            tmp.replace(expanded)
            return str(expanded)

        try:
            expanded = await asyncio.to_thread(_write_sync)
            logger.debug(
                "[AuthOrchestrator] wrote auth URL to %s",
                expanded,
            )
            return True
        except OSError as e:
            logger.error(
                "[AuthOrchestrator] failed to write %s: %s",
                expanded,
                e,
            )
            return False
