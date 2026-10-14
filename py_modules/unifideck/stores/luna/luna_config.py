"""LunaConfig: typed view over the plugin config for Luna (profile dir,
browser candidates, sign-in / detail / player URLs, stream hosts, spoof
settings). Built via ``LunaConfig.from_config_manager``.

Mirrors ``microsoft_config.py`` but carries **no** OAuth keys: Luna signs
in with browser session cookies only, so there is no ``client_id``, no
authorize/token endpoint, no redirect URI, no scope and no token file
(Req 12.3). It also carries no games.map-specific launch-routing key —
in 0.7.7 launch routing is the ``browser`` tag plus
``metadata["browser_url"]`` in ``library_cache.json``. Instead it reads
``allowed_stream_hosts`` for the ``is_allowed_browser_url`` allow-list and
resolves the browser-game CDP flavour.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from unifideck.auth.edge_browser.edge import WEB_GAME_PROFILE_DIR
from unifideck.stores.shared.config_reader import StoreConfigReader

if TYPE_CHECKING:
    from unifideck.config import ConfigManager

logger = logging.getLogger(__name__)

_LUNA_CONFIG_PREFIX = "stores.luna"

#: The spoofed User-Agent template must leave the browser version as a
#: runtime-substituted placeholder (Req 12.6, 14.8); a hardcoded version
#: lets Luna reject the client as outdated.
_VERSION_PLACEHOLDER = "{version}"


@dataclass(frozen=True)
class LunaConfig:
    """Luna config resolved from ``stores.luna`` in settings.json.

    All endpoints, browser parameters and spoofing parameters live here so
    they can change without code changes (Req 12.1). No OAuth keys are
    read (Req 12.3).
    """

    luna_host: str = ""
    allowed_stream_hosts: list[str] = field(default_factory=list)
    signin_path: str = ""
    detail_url_pattern: str = ""
    player_path: str = ""
    catalog_endpoint: str = ""
    subscription_endpoint: str = ""
    profile_dir: str | None = None
    browser_game_cdp_flavour: str = ""
    browser_candidates: list[str] = field(default_factory=list)
    cloud_browser_flags: list[str] = field(default_factory=list)
    gamepad_flags: list[str] = field(default_factory=list)
    flatpak_override_paths: list[str] = field(default_factory=list)
    spoof_ua_template: str = ""
    spoof_uach_platform: str = ""
    spoof_navigator_platform: str = ""
    spoof_webgl_renderer: str = ""

    @classmethod
    def from_config_manager(cls, config: ConfigManager | None) -> LunaConfig:
        """Read the Luna config under ``stores.luna``.

        Reads no OAuth keys (Req 12.3) and no games.map launch-routing key.
        ``profile_dir`` defaults to the shared ``WEB_GAME_PROFILE_DIR`` when
        unset (Req 12.8).
        """
        cfg = StoreConfigReader(config, _LUNA_CONFIG_PREFIX)
        profile_dir = cfg.text("profile_dir") or WEB_GAME_PROFILE_DIR
        return cls(
            luna_host=cfg.text("luna_host"),
            allowed_stream_hosts=cfg.text_list("allowed_stream_hosts"),
            signin_path=cfg.text("signin_path"),
            detail_url_pattern=cfg.text("detail_url_pattern"),
            player_path=cfg.text("player_path"),
            catalog_endpoint=cfg.text("catalog_endpoint"),
            subscription_endpoint=cfg.text("subscription_endpoint"),
            profile_dir=profile_dir,
            browser_game_cdp_flavour=cfg.text("browser_game_cdp_flavour"),
            browser_candidates=cfg.text_list("browser_candidates"),
            cloud_browser_flags=cfg.text_list("cloud_browser_flags"),
            gamepad_flags=cfg.text_list("gamepad_flags"),
            flatpak_override_paths=cfg.text_list("flatpak_override_paths"),
            spoof_ua_template=cfg.text("spoof_ua_template"),
            spoof_uach_platform=cfg.text("spoof_uach_platform"),
            spoof_navigator_platform=cfg.text("spoof_navigator_platform"),
            spoof_webgl_renderer=cfg.text("spoof_webgl_renderer"),
        )

    def is_valid(self) -> tuple[bool, list[str]]:
        """Whether every required key is set, and the names of those not.

        A key is invalid when it is missing, empty, or malformed for its
        type (Req 12.4, 12.5). ``spoof_ua_template`` is additionally
        malformed when it lacks the ``{version}`` placeholder, since a
        fixed version lets Luna reject the client as outdated (Req 12.6).
        """
        invalid: list[str] = []
        for name, text_val in self._required_text().items():
            if not text_val:
                invalid.append(name)
        for name, list_val in self._required_lists().items():
            if not list_val:
                invalid.append(name)
        if self.spoof_ua_template and _VERSION_PLACEHOLDER not in self.spoof_ua_template:
            invalid.append("spoof_ua_template")
        if invalid:
            logger.warning(
                "[LunaConfig] missing or invalid required keys: %s",
                ", ".join(invalid),
            )
        return (not invalid, invalid)

    def _required_text(self) -> dict[str, str]:
        return {
            "luna_host": self.luna_host,
            "signin_path": self.signin_path,
            "detail_url_pattern": self.detail_url_pattern,
            "player_path": self.player_path,
            # catalog_endpoint / subscription_endpoint are intentionally NOT
            # required: they are Strategy-A (authenticated API) placeholders,
            # unused by the default Strategy-B DOM scrape (design Q4 / task
            # 8.8 deferred). Requiring them wrongly blocked sign-in on a
            # scrape-only config.
            "profile_dir": self.profile_dir or "",
            "browser_game_cdp_flavour": self.browser_game_cdp_flavour,
            "spoof_ua_template": self.spoof_ua_template,
            "spoof_uach_platform": self.spoof_uach_platform,
            "spoof_navigator_platform": self.spoof_navigator_platform,
            "spoof_webgl_renderer": self.spoof_webgl_renderer,
        }

    def _required_lists(self) -> dict[str, list[str]]:
        return {
            "allowed_stream_hosts": self.allowed_stream_hosts,
            "browser_candidates": self.browser_candidates,
        }

    def describe(self) -> str:
        """Describe."""
        return (
            f"LunaConfig(luna_host={self.luna_host!r}, "
            f"profile_dir={self.profile_dir}, "
            f"browser_game_cdp_flavour={self.browser_game_cdp_flavour!r}, "
            f"browser_candidates={self.browser_candidates})"
        )
