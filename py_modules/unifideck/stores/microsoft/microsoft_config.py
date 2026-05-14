"""Microsoft store configuration — frozen dataclass with OAuth endpoints, scopes, token file path."""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unifideck.utils.config_helpers import get_cfg
if TYPE_CHECKING:
    from ...config import ConfigManager
logger = logging.getLogger(__name__)
_MS_CONFIG_PREFIX = "stores.microsoft"
_DEFAULT_TOKEN_FILE = "~/.local/share/unifideck/microsoft_tokens.json"
@dataclass(frozen=True)
class MicrosoftConfig:
    """Frozen Microsoft store configuration.

    Holds OAuth endpoints (auth_url, token_url, redirect
    URIs), XBL/XSTS endpoints, xCloud catalog URLs, and
    tunables (refresh threshold, user-agents, token file).
    Built from a ``ConfigManager`` via ``from_config_manager``.
    """
    client_id: str = ""
    scope: str = ""
    auth_url: str = ""
    token_url: str = ""
    redirect_uri: str = ""
    allowed_redirect_uris: list[str] = field(default_factory=list)
    xbl_auth_url: str = ""
    xsts_url: str = ""
    xcloud_catalog_url: str = ""
    xcloud_titles_url: str = ""
    xcloud_launch_url: str = ""
    gssv_relying_party: str = "http://gssv.xboxlive.com/"
    subscription_check_url: str = (
    "https://xgpuweb.gssv-play-prod.xboxlive.com/v2/login/user"
    )
    token_file: str = _DEFAULT_TOKEN_FILE
    token_refresh_threshold_seconds: int = 2400
    xbl_user_agent: str = "XboxReplay; XboxLiveAuth/3.0"
    catalog_user_agent: str = "Unifideck/1.0"

    @classmethod
    def from_config_manager(cls, config: ConfigManager | None) -> MicrosoftConfig:
        """Build a ``MicrosoftConfig`` from a ConfigManager (or defaults).

        Reads every key under ``stores.microsoft.*``. Missing
        keys produce empty strings (or sensible defaults for
        tunables). ``allowed_redirect_uris`` falls back to
        ``[redirect_uri]`` if not explicitly configured.

        Args:
            config: ConfigManager, or ``None`` (yields a
                ``MicrosoftConfig`` with empty required fields).

        Returns:
            Fully-populated ``MicrosoftConfig``.
        """
        def _s(key: str, default: str = "") -> str:
            """Read a stripped string from the Microsoft config namespace.

            Args:
                key: Suffix appended to ``stores.microsoft.``.
                default: Default returned if the key is missing.

            Returns:
                Stripped value or default.
            """
            val = get_cfg(config, f"{_MS_CONFIG_PREFIX}.{key}", default)
            return str(val).strip() if val is not None else default
        def _i(key: str, default: int) -> int:
            """Read an int from the Microsoft config namespace, defaulting on parse failure.

            Args:
                key: Suffix appended to ``stores.microsoft.``.
                default: Default returned on missing key or parse failure.

            Returns:
                Integer value.
            """
            val = get_cfg(config, f"{_MS_CONFIG_PREFIX}.{key}", default)
            try:
                return int(val)
            except (TypeError, ValueError):
                return default
        def _list(key: str) -> list[str]:
            """Read a list-of-strings from the Microsoft config namespace.

            Filters out non-string and empty entries.

            Args:
                key: Suffix appended to ``stores.microsoft.``.

            Returns:
                Non-empty string list (empty list if the key is
                missing or non-list).
            """
            val = get_cfg(config, f"{_MS_CONFIG_PREFIX}.{key}", None)
            if not isinstance(val, list):
                return []
            return [str(x) for x in val if isinstance(x, str) and x]
        primary_redirect = _s("redirect_uri")
        allowed = _list("allowed_redirect_uris")
        if not allowed and primary_redirect:
            allowed = [primary_redirect]
        return cls(
            client_id=_s("client_id"),
            scope=_s("scope"),
            auth_url=_s("auth_url"),
            token_url=_s("token_url"),
            redirect_uri=primary_redirect,
            allowed_redirect_uris=allowed,
            xbl_auth_url=_s("xbl_auth_url"),
            xsts_url=_s("xsts_url"),
            xcloud_catalog_url=_s("xcloud_catalog_url"),
            xcloud_titles_url=_s("xcloud_titles_url"),
            xcloud_launch_url=_s("xcloud_launch_url"),
            gssv_relying_party=_s(
                "gssv_relying_party", "http://gssv.xboxlive.com/",
            ),
            subscription_check_url=_s(
                "subscription_check_url",
                "https://xgpuweb.gssv-play-prod.xboxlive.com/v2/login/user",
            ),
            token_file=_s("token_file", _DEFAULT_TOKEN_FILE),
            token_refresh_threshold_seconds=_i(
                "token_refresh_threshold_seconds", 2400,
            ),
            xbl_user_agent=_s(
                "xbl_user_agent",
                "XboxReplay; XboxLiveAuth/3.0",
            ),
            catalog_user_agent=_s(
                "catalog_user_agent", "Unifideck/1.0",
            ),
        )

    def is_valid(self) -> bool:

        """Check whether every required URL / scope is non-empty.

        Logs the list of missing keys at WARNING.

        Returns:
            True iff all required fields are populated.
        """
        required = (
            self.client_id,
            self.scope,
            self.auth_url,
            self.token_url,
            self.redirect_uri,
            self.xbl_auth_url,
            self.xsts_url,
            self.xcloud_catalog_url,
            self.xcloud_launch_url,
        )
        missing = [
            name for name, val in zip(
                (
                    "client_id", "scope", "auth_url", "token_url",
                    "redirect_uri", "xbl_auth_url", "xsts_url",
                    "xcloud_catalog_url", "xcloud_launch_url",
                ),
                required, strict=False,
            )
            if not val
        ]
        if missing:
            logger.warning(
                "[MicrosoftConfig] missing required keys: %s",
                ", ".join(missing),
            )
            return False
        return True
    def describe(self) -> str:
        """Build a short, redacted description suitable for logs.

        Returns:
            String like ``MicrosoftConfig(client_id=abc123…, ...)``.
        """
        return (
            f"MicrosoftConfig(client_id={self.client_id[:6]}…, "
            f"scope={self.scope!r}, "
            f"token_file={self.token_file})"
        )