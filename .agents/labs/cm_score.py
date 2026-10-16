# SPDX-License-Identifier: MIT
"""The context-management lab's scorer: ``.venv/bin/python -I .agents/labs/cm_score.py
BUNDLE...`` (``cm_lab/score.py``; the runbook is ``cm-lab-round-1.md``)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cm_lab.score import main

if __name__ == "__main__":
    sys.exit(main())
