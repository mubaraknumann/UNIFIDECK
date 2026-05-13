"""GOG user info dataclass — minimal display + identity fields.

OP-22-gog-tokens-userinfo | py_modules/unifideck/stores/gog/tokens/user_info.py

Two fields:

* ``username`` — display name (shown in the UI);
* ``galaxy_user_id`` — Galaxy account id (used
  for per-user paths in gogdl).

Mutable on purpose: the token manager updates it
on each successful refresh.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GOGUserInfo:
    """Per-user GOG account info bundled alongside tokens.

    Both fields default to empty strings so the
    dataclass can be constructed before user info
    has been resolved (rare edge case after a
    successful auth but before the
    ``/userData.json`` call).
    """

    username: str = ""
    galaxy_user_id: str = ""
