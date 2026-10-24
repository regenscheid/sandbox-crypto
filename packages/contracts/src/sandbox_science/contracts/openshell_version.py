# SPDX-License-Identifier: MIT
"""The one OpenShell release Sandbox Science is built and tested against.

The gateway, the CLI, the supervisor and the VM driver are versioned together, and the
policy compiler, the worker's CLI calls, the middleware's protobuf contracts and the
Kubernetes driver config follow that release's behavior. Everything that names the
release imports or reads this constant: the launcher refuses another CLI at start, the
trusted images' check compares ``openshell --version`` with it, and the babbage and Mac
scripts read this file (``scripts/personal_up.sh`` with ``sed``, ``scripts/mac_local.py``
with a regular expression, since neither can import the package). Documentation names the
release in prose; ``packages/contracts/tests/test_openshell_version.py`` fails when a
version literal appears in code instead.

To move to another release, change ``OPENSHELL_VERSION`` here, then ``OPENSHELL_VERSION``
in ``images/Containerfile``, the installer digest in ``scripts/mac_local.py`` and the
checksums and protobuf contracts the tests name; the agreement tests list what is left.
"""

from __future__ import annotations

import re
from typing import Final

OPENSHELL_VERSION: Final = "0.1.5"
OPENSHELL_VERSION_OUTPUT: Final = f"openshell {OPENSHELL_VERSION}"
"""What ``openshell --version`` prints for the pinned release."""

_PINNED: Final = re.compile(rf"(?<![\w.])v?{re.escape(OPENSHELL_VERSION)}(?![\w.+-])")


def pinned_version(output: str) -> bool:
    """Whether a ``--version`` output names the pinned release: ``openshell 0.1.5`` or
    ``openshell-gateway v0.1.5``, not ``0.1.50`` or a prerelease of it."""
    return _PINNED.search(output) is not None
