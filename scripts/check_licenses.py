# SPDX-License-Identifier: MIT
"""Fail when an installed Python distribution's license is outside the tier 1 allowlist.

docs/LICENSING.md defines the tiers. Run inside the project environment, as CI
does: ``uv run python scripts/check_licenses.py``.
"""

from __future__ import annotations

import re
import sys
from importlib.metadata import Distribution, distributions

ALLOWED = frozenset(
    {
        "0BSD",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "CC0-1.0",
        "ISC",
        "MIT",
        "MIT-0",
        "MPL-2.0",
        "PSF-2.0",
        "PostgreSQL",
        "Unlicense",
    }
)

# Short values of the legacy ``License`` metadata field, mapped to SPDX identifiers.
LICENSE_FIELD = {
    "MIT": "MIT",
    "MIT License": "MIT",
    "BSD": "BSD-3-Clause",
    "BSD License": "BSD-3-Clause",
    "3-Clause BSD License": "BSD-3-Clause",
    "Apache-2.0": "Apache-2.0",
    "Apache 2.0": "Apache-2.0",
    "Apache License 2.0": "Apache-2.0",
    "ISC": "ISC",
}

CLASSIFIERS = {
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: BSD License": "BSD-3-Clause",
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
}


def license_of(dist: Distribution) -> str | None:
    """Return the distribution's license as an SPDX expression, if it can be determined."""
    metadata = dist.metadata
    expression = metadata.get("License-Expression")
    if expression:
        return expression
    field = (metadata.get("License") or "").strip()
    if field in LICENSE_FIELD:
        return LICENSE_FIELD[field]
    if field in ALLOWED:
        return field  # already an SPDX identifier, as psutil writes it
    for classifier in metadata.get_all("Classifier") or []:
        if classifier in CLASSIFIERS:
            return CLASSIFIERS[classifier]
    return None


def is_allowed(expression: str) -> bool:
    """Evaluate a flat SPDX expression (``OR`` of ``AND`` terms) against the allowlist."""
    alternatives = re.split(r"\s+OR\s+", expression.strip().strip("()"))
    return any(
        all(term.strip().strip("()") in ALLOWED for term in re.split(r"\s+AND\s+", alternative))
        for alternative in alternatives
    )


def main() -> int:
    problems: list[str] = []
    for dist in sorted(distributions(), key=lambda dist: dist.metadata["Name"].lower()):
        name = dist.metadata["Name"]
        expression = license_of(dist)
        if expression is None:
            problems.append(f"{name}: license not declared in metadata")
        elif not is_allowed(expression):
            problems.append(f"{name}: {expression} is not allowed in tier 1")
    for problem in problems:
        print(problem, file=sys.stderr)
    if not problems:
        print("all installed distributions have tier 1 licenses")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
