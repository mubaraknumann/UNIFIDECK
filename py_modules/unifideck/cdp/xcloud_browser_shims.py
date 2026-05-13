"""JS shim payloads injected into the xCloud browser tab.

OP-13d | py_modules/unifideck/cdp/xcloud_browser_shims.py

Hosts the JavaScript blobs that the plugin's xCloud
launcher injects into the Edge / Chromium tab via
``page_inject.inject_scripts``. Two payloads:

* ``_XCLOUD_BROWSER_SHIMS_JS`` — the runtime shim
  installed once on the page. It:
    - Spoofs the user-agent to look like Edge on
      Windows (xCloud refuses non-Edge UAs);
    - Stubs fullscreen + pointer-lock APIs that the
      page expects to succeed;
    - Normalises gamepads to a Standard mapping
      (xCloud only recognises Standard-mapping
      controllers and a specific Xbox id) via a
      Proxy + ``getGamepads`` override;
    - Sets up periodic gamepad resync events to
      handle the case where the gamepad connects
      after the page loaded.
* The navigation helper (``get_xcloud_navigation_js``)
  asks the page to redirect to a target URL once,
  used to deep-link into a specific stream.

The JS is kept as a Python string literal (rather than
external ``.js`` file) so the whole plugin ships as one
artifact.
"""

import json

_XCLOUD_BROWSER_SHIMS_JS = r"""
(function() {
    'use strict';
    if (window.__unifideck_xcloud_helper) return;
    var state = {
        injectedAt: Date.now(),
        reconnects: 0,
        listenerRegistrations: 0,
        lastReason: 'init',
    };
    window.__unifideck_xcloud_helper = state;
    var XBOX_GAMEPAD_ID = 'Xbox 360 Controller (XInput STANDARD GAMEPAD)';
    var defaultChromiumVersion = '120.0.0.0';
    var proxyCache = typeof WeakMap === 'function' ? new WeakMap() : null;
    var originalGetGamepads =
        typeof navigator.getGamepads === 'function'
            ? navigator.getGamepads.bind(navigator)
            : null;
    var originalWebkitGetGamepads =
        typeof navigator.webkitGetGamepads === 'function'
            ? navigator.webkitGetGamepads.bind(navigator)
            : null;
    function getChromiumVersion() {
        try {
            var ua = String(navigator.userAgent || '');
            var match = ua.match(/\s(?:Chrome|Edg)\/([\d.]+)/);
            if (match && match[1]) return match[1];
        } catch (e) {}
        return defaultChromiumVersion;
    }
    function spoofBrowserIdentity() {
        var chromiumVersion = getChromiumVersion();
        var edgeUserAgent =
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) ' +
            'AppleWebKit/537.36 (KHTML, like Gecko) ' +
            'Chrome/' + chromiumVersion + ' Safari/537.36 Edg/' + chromiumVersion;
        state.originalUserAgent = String(navigator.userAgent || '');
        state.spoofedUserAgent = edgeUserAgent;
        try {
            Object.defineProperty(navigator, 'userAgent', {
                configurable: true,
                get: function() { return edgeUserAgent; },
            });
            Object.defineProperty(navigator, 'platform', {
                configurable: true,
                get: function() { return 'Win32'; },
            });
        } catch (e) {}
    }
    var pointerLockElement = null;
    try {
        Object.defineProperty(document, 'fullscreenElement', {
            configurable: true,
            get: function() { return document.documentElement; },
        });
    } catch (e) {}
    try {
        if (typeof HTMLElement.prototype.requestFullscreen !== 'function') {
            HTMLElement.prototype.requestFullscreen = function() {
                return Promise.resolve();
            };
        }
    } catch (e) {}
    try {
        Object.defineProperty(document, 'pointerLockElement', {
            configurable: true,
            get: function() { return pointerLockElement; },
        });
    } catch (e) {}
    try {
        HTMLElement.prototype.requestPointerLock = function() {
            pointerLockElement = document.documentElement;
            document.dispatchEvent(new Event('pointerlockchange'));
        };
    } catch (e) {}
    try {
        document.exitPointerLock = function() {
            pointerLockElement = null;
            document.dispatchEvent(new Event('pointerlockchange'));
        };
    } catch (e) {}
    function shouldSpoofGamepad(gamepad) {
        if (!gamepad || !gamepad.connected) return false;
        var id = String(gamepad.id || '');
        return !(id.indexOf('Xbox') !== -1 && gamepad.mapping === 'standard');
    }
    function normalizeGamepad(gamepad) {
        if (!shouldSpoofGamepad(gamepad)) return gamepad;
        if (proxyCache && proxyCache.has(gamepad)) return proxyCache.get(gamepad);
        var wrapped = null;
        try {
            wrapped = new Proxy(gamepad, {
                get: function(target, prop, receiver) {
                    if (prop === 'id') return XBOX_GAMEPAD_ID;
                    if (prop === 'mapping') return 'standard';
                    return Reflect.get(target, prop, receiver);
                },
            });
        } catch (e) {
            wrapped = gamepad;
        }
        if (proxyCache) proxyCache.set(gamepad, wrapped);
        return wrapped;
    }
    function remapGamepadArray(gamepads) {
        var result = [];
        for (var i = 0; i < gamepads.length; i += 1) {
            result[i] = gamepads[i] ? normalizeGamepad(gamepads[i]) : null;
        }
        return result;
    }
    function installGamepadOverride() {
        if (originalGetGamepads) {
            try {
                Object.defineProperty(navigator, 'getGamepads', {
                    configurable: true,
                    writable: true,
                    value: function() {
                        return remapGamepadArray(originalGetGamepads() || []);
                    },
                });
            } catch (e) {}
        }
    }
    spoofBrowserIdentity();
    installGamepadOverride();
})();
"""


def get_xcloud_browser_shims_js() -> str:
    """Return the full xCloud browser-shim JS payload.

    Single accessor for the long string literal — keeps
    the call sites short and provides a single place to
    add per-deployment substitutions if ever needed.

    Returns:
        JS source as a string.
    """
    return _XCLOUD_BROWSER_SHIMS_JS


def get_xcloud_navigation_js(target_url: str) -> str:
    """Build a small JS payload that navigates the tab to ``target_url``.

    The generated IIFE:

    * Stores the URL on
      ``window.__unifideck_xcloud_target_url`` for
      debugging visibility;
    * Skips navigation if already on the target;
    * Schedules a 250 ms-delayed
      ``window.location.assign`` so the redirect
      doesn't race the rest of the shim injection.

    The URL is JSON-encoded for safe embedding (handles
    quotes / escape sequences correctly).

    Args:
        target_url: full URL to navigate to.

    Returns:
        JS IIFE source, or empty string when no URL.
    """
    if not target_url:
        return ""
    encoded_target = json.dumps(target_url)
    return f"""
                            (function() {{
                                'use strict';
                                var targetUrl = {encoded_target};
                                if (!targetUrl) return;
                                window.__unifideck_xcloud_target_url = targetUrl;
                                if (window.location.href === targetUrl) return;
                                window.setTimeout(function() {{
                                    try {{
                                        if (window.location.href !== targetUrl) {{
                                            window.location.assign(targetUrl);
                                        }}
                                    }} catch (e) {{}}
                                }}, 250);
                            }})();
                            """
