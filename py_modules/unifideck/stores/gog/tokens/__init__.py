"""GOG token management — public re-exports.

OP-22-gog-tokens-init | py_modules/unifideck/stores/gog/tokens/__init__.py

Bundles the GOG token subsystem behind two public
names:

* ``GOGTokenManager`` — orchestration (OAuth
  exchange, refresh, persistence);
* ``GOGUserInfo`` — small dataclass with the
  user's display info.

Implementation is split across:

* ``manager`` — high-level orchestration;
* ``oauth`` — pure HTTP for code/refresh
  exchange;
* ``storage`` — encrypted on-disk persistence
  via ``SecureTokenStore``;
* ``gogdl_credentials`` — write the gogdl
  credentials JSON file so subprocess gogdl runs
  see the user's session.
"""

from .manager import GOGTokenManager
from .user_info import GOGUserInfo

__all__ = ["GOGTokenManager", "GOGUserInfo"]
