# SPDX-License-Identifier: MIT
"""Verified client contexts for private deployment CAs, added to system trust."""

from __future__ import annotations

import ssl
from pathlib import Path


def client_tls(ca_file: Path | None = None) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if ca_file is not None:
        context.load_verify_locations(cafile=ca_file)
    return context
