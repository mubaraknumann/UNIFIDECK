"""Security RPC handlers.

OP-25f | py_modules/unifideck/rpc/handlers/security.py
"""
from __future__ import annotations

import logging
from typing import Any

from unifideck.rpc.handlers.base import RpcHandlerBase

logger = logging.getLogger(__name__)


class SecurityHandlers(RpcHandlerBase):
    """Security audit log, counters, and brute-force management."""

    def _security(self) -> Any:
        """Return the security service, raising RpcError if unavailable.

        Returns:
            The security service.

        Raises:
            RpcError: ``service_unavailable`` when the security
                service isn't wired.
        """
        return self._require(self._services.security, "security")

    async def get_security_audit_log(self, limit: int = 100) -> Any:
        """Return the last N entries from the security audit log.

        Args:
            limit: Max number of entries (tail-bias). Default 100.

        Returns:
            List of audit-log entry dicts (newest first).
        """
        return self._security().get_audit_log(limit=limit)

    async def get_security_counters(self) -> Any:
        """Return security event counters."""
        return self._security().get_counters()

    async def get_security_bruteforce_status(self) -> Any:
        """Return current brute-force lockout state."""
        return self._security().get_bruteforce_status()

    async def clear_security_audit_log(self) -> Any:
        """Clear the security audit log.

        Wipes every recorded event. Used by the UI's privacy
        controls.

        Returns:
            None — present for RPC return-type uniformity.
        """
        self._security().clear_audit_log()
        return {"success": True}

    async def reset_security_bruteforce(self) -> Any:
        """Reset brute-force lockout counters."""
        self._security().reset_bruteforce_state()
        return {"success": True}
