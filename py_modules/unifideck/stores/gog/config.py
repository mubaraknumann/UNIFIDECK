"""Strongly-typed GOG store config block, loaded from ConfigManager.

OP-22-gog-config | py_modules/unifideck/stores/gog/config.py

Wraps the ``stores.gog.*`` config entries behind a
frozen dataclass. Same pattern as
``MicrosoftConfig`` but with GOG-specific fields:

* OAuth (client_id, client_secret, auth/token URLs);
* GOG API roots (``base_url``, ``api_gog_url``) for
  library + catalog;
* gogdl integration paths (``gogdl_config_dir``
  where we write the credentials JSON file that
  gogdl reads);
* ``download_dir`` — default install location;
* ``supported_languages`` — languages the install
  planner should consider for the user's locale.

The auth URL file constant (``GOG_AUTH_URL_FILE``)
is module-level since the auth flow writes it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from unifideck.utils.config_helpers import get_cfg

if TYPE_CHECKING:
    from ...config import ConfigManager

logger = logging.getLogger(__name__)

_GOG_CONFIG_PREFIX = "stores.gog"
_DEFAULT_TOKEN_FILE = "~/.config/unifideck/gog_token.json"
_DEFAULT_GOGDL_CONFIG_DIR = "~/.config/unifideck/gogdl"
_DEFAULT_DOWNLOAD_DIR = "~/GOG Games"
GOG_AUTH_URL_FILE = "~/.local/share/unifideck/gog_auth_url.txt"


@dataclass(frozen=True)
class GOGConfig:
    """Frozen value-object holding the GOG store's runtime configuration.

    Frozen so it can be passed around without
    fear of mutation. Default values for the
    fields that have sensible defaults (paths,
    languages, UA); empty defaults for the
    required OAuth fields so ``is_valid`` can
    detect "not configured".
    """

    client_id: str = ""
    client_secret: str = ""
    auth_url: str = ""
    token_url: str = ""
    redirect_uri: str = ""
    allowed_redirect_uris: list[str] = field(default_factory=list)
    base_url: str = ""
    api_gog_url: str = ""
    token_file: str = _DEFAULT_TOKEN_FILE
    gogdl_config_dir: str = _DEFAULT_GOGDL_CONFIG_DIR
    download_dir: str = _DEFAULT_DOWNLOAD_DIR
    token_refresh_threshold_seconds: int = 2400
    supported_languages: list[str] = field(
        default_factory=lambda: ["en", "de", "fr", "pl", "ru", "pt", "es", "it", "zh", "ko", "ja"],
    )
    user_agent: str = "Unifideck/1.0"

    @classmethod
    def from_config_manager(cls, config: ConfigManager | None) -> GOGConfig:
        """Build a ``GOGConfig`` from the plugin's ConfigManager.

        Same three inner helpers as
        ``MicrosoftConfig.from_config_manager`` —
        ``_s`` (string + trim), ``_i`` (int +
        safe coerce), ``_list`` (list of non-empty
        strings).

        Post-load:

        * ``allowed_redirect_uris`` fallback — if
          no list configured but
          ``redirect_uri`` set, use it as the
          singleton allowed value;
        * ``supported_languages`` fallback — if
          empty, use the hard-coded 11-language
          default that covers the major regions.

        Args:
            config: ``ConfigManager`` or ``None``.

        Returns:
            New ``GOGConfig``.
        """

        def _str(key: str, default: str = "") -> str:
            """Read string config with trim, tolerate None.

            Args:
                key: relative key.
                default: fallback.

            Returns:
                Trimmed string.
            """
            val = get_cfg(config, f"{_GOG_CONFIG_PREFIX}.{key}", default)
            return str(val).strip() if val is not None else default

        def _int(key: str, default: int) -> int:
            """Read int config with safe coercion.

            Args:
                key: relative key.
                default: fallback.

            Returns:
                Parsed int.
            """
            val = get_cfg(config, f"{_GOG_CONFIG_PREFIX}.{key}", default)
            try:
                return int(val)
            except (TypeError, ValueError):
                return default

        def _list(key: str) -> list[str]:
            """Read list-of-string config, filter empties.

            Args:
                key: relative key.

            Returns:
                List of strings.
            """
            val = get_cfg(config, f"{_GOG_CONFIG_PREFIX}.{key}", None)
            if not isinstance(val, list):
                return []
            return [str(x) for x in val if isinstance(x, str) and x]

        primary_redirect = _s("redirect_uri")
        allowed = _list("allowed_redirect_uris")
        if not allowed and primary_redirect:
            allowed = [primary_redirect]
        supported = _list("supported_languages")
        if not supported:
            supported = ["en", "de", "fr", "pl", "ru", "pt", "es", "it", "zh", "ko", "ja"]
        return cls(
            client_id=_s("client_id"),
            client_secret=_s("client_secret"),
            auth_url=_s("auth_url"),
            token_url=_s("token_url"),
            redirect_uri=primary_redirect,
            allowed_redirect_uris=allowed,
            base_url=_s("base_url"),
            api_gog_url=_s("api_gog_url"),
            token_file=_s("token_file", _DEFAULT_TOKEN_FILE),
            gogdl_config_dir=_s(
                "gogdl_config_dir",
                _DEFAULT_GOGDL_CONFIG_DIR,
            ),
            download_dir=_s("download_dir", _DEFAULT_DOWNLOAD_DIR),
            token_refresh_threshold_seconds=_i(
                "token_refresh_threshold_seconds",
                2400,
            ),
            supported_languages=supported,
            user_agent=_s("user_agent", "Unifideck/1.0"),
        )

    def is_valid(self) -> bool:
        """Check all required OAuth + API URLs are configured.

        Seven mandatory fields. Logs missing names
        at WARN. Doesn't validate the format of the
        URLs — that's the OAuth call's job.

        Returns:
            True iff all required fields non-empty.
        """
        required = (
            ("client_id", self.client_id),
            ("client_secret", self.client_secret),
            ("auth_url", self.auth_url),
            ("token_url", self.token_url),
            ("redirect_uri", self.redirect_uri),
            ("base_url", self.base_url),
            ("api_gog_url", self.api_gog_url),
        )
        missing = [name for name, val in required if not val]
        if missing:
            logger.warning(
                "[GOGConfig] missing required keys: %s",
                ", ".join(missing),
            )
            return False
        return True

    @property
    def auth_config_path(self) -> str:
        """Compute the gogdl credentials file path (under ``gogdl_config_dir``).

        gogdl reads credentials from
        ``<config_dir>/gog_credentials.json``;
        the token manager writes this file on
        successful auth so subprocess gogdl runs
        pick up the user's session.

        Returns:
            Absolute path string (with ``~``
            expanded).
        """
        import os

        return os.path.join(os.path.expanduser(self.gogdl_config_dir), "gog_credentials.json")

    def describe(self) -> str:
        """Return a short human-readable summary for logging.

        Truncates client_id to 6 chars to avoid
        leaking the full OAuth client id.

        Returns:
            Description string.
        """
        return (
            f"GOGConfig(client_id={self.client_id[:6]}…, "
            f"base_url={self.base_url}, "
            f"token_file={self.token_file}, "
            f"download_dir={self.download_dir})"
        )
