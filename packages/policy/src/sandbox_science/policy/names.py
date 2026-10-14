# SPDX-License-Identifier: MIT
"""Package names as their ecosystems compare them."""

from __future__ import annotations

import re
from typing import Final

_SEPARATORS: Final = re.compile(r"[-_.]+")


def pep503(name: str) -> str:
    """A Python project name as PEP 503 compares it: lower case, runs of ``-_.`` as ``-``."""
    return _SEPARATORS.sub("-", name).lower()
