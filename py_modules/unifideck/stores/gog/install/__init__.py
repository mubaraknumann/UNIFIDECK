"""GOG install pipeline — public re-exports.

OP-22-gog-install-init | py_modules/unifideck/stores/gog/install/__init__.py

The install pipeline is broken into focused
modules:

* ``installer`` — public ``GOGInstaller`` class
  orchestrating the install flow;
* ``planner`` — choose gogdl args (language,
  branch, manifest, install path);
* ``progress`` — parse gogdl stdout for progress
  events;
* ``marker`` — write/read the install marker
  file that proves an install is complete;
* ``helpers`` — small utilities (path
  resolution, dir prep);
* ``primitives`` — folder operations (size,
  count, cleanup);
* ``languages`` — locale → gogdl language
  matching;
* ``uninstall_pipeline`` — uninstall flow.
"""

from .installer import GOGInstaller

__all__ = ["GOGInstaller"]
