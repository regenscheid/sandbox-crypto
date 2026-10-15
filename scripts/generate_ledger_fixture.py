# SPDX-License-Identifier: MIT
"""Write what the browser's ledger views must agree with the ledger on, from its models.

The ledger's types and their statuses, its link kinds and the names of the fields each type
gives its entries are values in ``orchestrator/ledger/models.py``, not types the API's
schema carries, so ``generate_api_types.py`` cannot give them to the browser. This script
writes them to ``web/workspace/src/lib/ledger.fixtures.json``, which the views' tests
compare with their own tables (``lib/ledger.ts``). Run
``uv run python scripts/generate_ledger_fixture.py`` after changing those models. With
``--check``, as the workspace tests run it, it changes nothing and fails when the file is
stale.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sandbox_science.orchestrator.ledger.models import FIELDS, LINKS, STATUSES

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "workspace" / "src" / "lib" / "ledger.fixtures.json"


def generate() -> str:
    fields = sorted({name for model in FIELDS.values() for name in model.model_fields})
    data = {
        "statuses": {kind: list(statuses) for kind, statuses in STATUSES.items()},
        "link_kinds": list(LINKS),
        "fields": fields,
    }
    return json.dumps(data, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Write the ledger views' fixture.")
    parser.add_argument("--check", action="store_true", help="fail when the file is stale")
    check = parser.parse_args().check
    text = generate()
    if check:
        if not OUT.is_file() or OUT.read_text(encoding="utf-8") != text:
            print(f"{OUT.relative_to(ROOT)} is stale: run scripts/generate_ledger_fixture.py")
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
