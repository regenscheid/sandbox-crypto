# SPDX-License-Identifier: MIT
"""Optional worker API server identity; clients still need their scoped bearer token."""

from __future__ import annotations

import ssl
import stat
from pathlib import Path

from sandbox_science.contracts.model import Contract


class ServerTLS(Contract):
    certificate: Path
    private_key: Path

    def context(self) -> ssl.SSLContext:
        """Load the configured identity without accepting projected/nonprivate keys."""
        mode = self.private_key.stat().st_mode
        if self.private_key.is_symlink() or not stat.S_ISREG(mode) or mode & 0o077:
            raise ValueError("worker TLS key must be a private regular file")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.certificate, self.private_key)
        return context
