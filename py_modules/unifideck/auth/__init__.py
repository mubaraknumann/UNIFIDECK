"""Auth — OAuth browser monitoring + Edge installer for xCloud login.

OP-15 | py_modules/unifideck/auth/__init__.py

Two sub-features:

* ``browser`` — generic OAuth redirect-capture via CDP
  target polling. Used by Epic / GOG / Ubisoft /
  Amazon login flows that open a browser tab and wait
  for the OAuth provider to redirect to the
  pre-registered callback URL.
* ``edge_browser/`` — Microsoft Edge auto-installer and
  launcher with isolated profile. Required for the
  Xbox / xCloud login flow which only works against
  Edge.
* ``orchestrator`` — high-level coordinator that
  chooses the browser, opens the auth URL, watches
  for redirect, returns the typed result.
"""

from .browser import CDPOAuthMonitor, OAuthBrowserMonitor

__all__ = ["CDPOAuthMonitor", "OAuthBrowserMonitor"]
