"""Amazon-store language setup — writes Windows locale registry values into the prefix."""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING
from .registry_io import _apply_windows_locale
from .resolver import get_unifideck_language
if TYPE_CHECKING:
    from ....config import ConfigManager
logger = logging.getLogger(__name__)
def apply_amazon_language(
    prefix_path: str, config: ConfigManager | None = None,
) -> bool:
    """Apply the user's preferred Windows locale to an Amazon prefix.

    Amazon Games has no per-store language settings of its own,
    so we only patch ``user.reg``'s Control Panel International
    section (same as the generic Windows-locale path).

    Args:
        prefix_path: Any path inside the prefix.
        config: ConfigManager (provides the user language).

    Returns:
        True iff user.reg was patched.
    """
    language = get_unifideck_language(config)
    logger.info(
        "[language_setup.amazon] applying %s to prefix=%s",
        language, prefix_path,
    )
    return _apply_windows_locale(prefix_path, language)