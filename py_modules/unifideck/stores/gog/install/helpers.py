"""Pre-install helpers — probe game info, parse gogdl output, choose languages.

OP-22-gog-install-helpers
File: py_modules/unifideck/stores/gog/install/helpers.py

Two responsibilities collected on
``_InstallHelpers``:

1. **Probe**: run ``gogdl info`` to discover the
   game's platform (Linux vs Windows fallback),
   folder name (gogdl picks this), and supported
   languages list. Linux is tried first; on
   non-zero exit we retry with Windows (Wine/Proton
   will run it).

2. **Language pick**: given the user's primary
   locale and the supported list, decide which
   language(s) to install. Two flavours:

   * ``explicit=True`` — user picked a specific
     language, install just that one (fallback to
     supported[0] if not available);
   * ``explicit=False`` — install primary +
     en-US fallback so the game has a usable
     default text.

A 60-second timeout on ``gogdl info`` protects
against hung subprocesses; on timeout we kill the
process and treat it as a probe failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING
from .languages import smart_match_language

if TYPE_CHECKING:
    from .installer import GOGInstaller

logger = logging.getLogger(__name__)


class _InstallHelpers:
    """Internal helpers used by ``GOGInstaller`` during install setup.

    Holds a back-reference to its parent so it
    can read the gogdl binary path + tokens +
    config. Kept separate from the installer
    itself for testability + readability.
    """

    def __init__(self, parent: GOGInstaller) -> None:
        """Stash the parent reference.

        Args:
            parent: ``GOGInstaller`` instance.
        """
        self._parent = parent

    async def probe_game_info(self, game_id: str) -> tuple[str, str | None, list[str]]:
        """Run ``gogdl info`` to discover platform + folder name + languages.

        Tries Linux first, Windows as fallback if
        the Linux build doesn't exist. For each
        attempt:

        1. Acquire gogdl credentials (tempdir +
           env);
        2. Spawn ``gogdl info`` with 60-second
           timeout;
        3. On timeout, kill the subprocess and
           continue (we'll either retry with
           Windows or give up);
        4. Always release the gogdl credentials
           via the cleanup callable (in
           ``finally``);
        5. On Linux failure, log + continue to
           Windows;
        6. On success, parse the JSON-lines
           output for ``folder_name`` and
           ``languages``.

        Args:
            game_id: GOG product id.

        Returns:
            ``(platform, folder_name,
            languages)`` triple. ``folder_name``
            is ``None`` on total failure.
        """
        platform = "linux"
        folder_name: str | None = None
        languages: list[str] = []
        for trial_platform in ("linux", "windows"):
            cmd = [
                self._parent._gogdl_bin,
                "--auth-config-path",
                self._parent._config.auth_config_path,
                "info",
                "--platform",
                trial_platform,
                game_id,
            ]
            env, _gogdl_cleanup = await self._parent._tokens.acquire_gogdl_creds()
            stdout = b""
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                try:
                    stdout, _stderr = await asyncio.wait_for(
                        proc.communicate(),
                        timeout=60,
                    )
                except TimeoutError:
                    logger.warning(
                        "[GOGInstaller] gogdl info timed out on "
                        "%s/%s — killing subprocess",
                        trial_platform,
                        game_id,
                    )
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await proc.wait()
                    stdout = b""
            finally:
                await _gogdl_cleanup()
            if proc.returncode != 0 and trial_platform == "linux":
                logger.info(
                    "[GOGInstaller] no Linux build for %s, trying Windows",
                    game_id,
                )
                continue
            platform = trial_platform
            folder_name, languages = self.parse_info_output(
                stdout.decode(errors="replace"),
            )
            break
        if folder_name:
            logger.info(
                "[GOGInstaller] info: platform=%s folder=%s langs=%s",
                platform,
                folder_name,
                languages,
            )
        return platform, folder_name, languages

    @staticmethod
    def parse_info_output(stdout: str) -> tuple[str | None, list[str]]:
        """Parse gogdl info's JSON-lines stdout for folder_name + languages.

        gogdl emits multiple JSON lines on stdout;
        we want the *latest* values for
        ``folder_name`` and ``languages``, so we
        iterate in *reverse* and take the first
        match for each.

        Non-JSON lines are skipped silently
        (gogdl mixes log lines with JSON in some
        versions). Returns whatever was found, or
        ``(None, [])`` if nothing matched.

        Args:
            stdout: full stdout text.

        Returns:
            ``(folder_name_or_None, languages)``.
        """
        folder_name: str | None = None
        languages: list[str] = []
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "folder_name" in data and not folder_name:
                folder_name = data["folder_name"]
            if "languages" in data and not languages:
                langs = data["languages"]
                if isinstance(langs, list):
                    languages = [str(x) for x in langs]
            if folder_name and languages:
                break
        return folder_name, languages

    @staticmethod
    def pick_languages(primary_lang: str, explicit: bool, supported: list[str]) -> list[str]:
        """Dispatch to explicit-vs-implicit language picker.

        Args:
            primary_lang: user's primary locale.
            explicit: True iff the user picked a
                specific language.
            supported: gogdl-reported supported
                languages.

        Returns:
            List of language codes to pass to
            gogdl.
        """
        if explicit:
            return _InstallHelpers._pick_explicit_lang(
                primary_lang,
                supported,
            )
        return _InstallHelpers._pick_implicit_langs(
            primary_lang,
            supported,
        )

    @staticmethod
    def _pick_explicit_lang(primary_lang: str, supported: list[str]) -> list[str]:
        """User picked a specific language — match it or fall back to first supported.

        If ``supported`` is empty (probe failed),
        trust the user's choice as-is. Otherwise,
        smart-match; if no match, warn and use
        the first available language so the
        install proceeds.

        Args:
            primary_lang: requested code.
            supported: available codes.

        Returns:
            Single-element list with the chosen
            code.
        """
        if not supported:
            return [primary_lang]
        matched = smart_match_language(primary_lang, supported)
        if matched:
            return [matched]
        logger.warning(
            "[GOGInstaller] %s not available, using %s",
            primary_lang,
            supported[0],
        )
        return [supported[0]]

    @staticmethod
    def _pick_implicit_langs(primary_lang: str, supported: list[str]) -> list[str]:
        """No explicit pick — install primary + English fallback for safety.

        If ``supported`` is empty, just return
        ``[primary_lang]`` plus ``"en-US"`` if not
        already there.

        With a supported list:

        1. Smart-match primary → add;
        2. No primary match → smart-match en-US
           as a fallback → add;
        3. No en-US match either → first
           supported as last resort.

        Returns just the matched-primary-or-
        fallback (single-element list) — we
        don't add a second english entry because
        users with stable connections paying for
        bandwidth shouldn't double-download.

        Args:
            primary_lang: user's locale.
            supported: gogdl languages.

        Returns:
            List with the chosen language(s).
        """
        if not supported:
            langs = [primary_lang]
            if "en-US" not in langs:
                langs.append("en-US")
            return langs
        result: list[str] = []
        matched = smart_match_language(primary_lang, supported)
        if matched:
            result.append(matched)
        else:
            matched_english = smart_match_language(
                "en-US",
                supported,
            )
            if matched_english:
                result.append(matched_english)
            else:
                result.append(supported[0])
        return result
