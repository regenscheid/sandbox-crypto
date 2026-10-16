# SPDX-License-Identifier: MIT
"""The context-management lab's driver: ``.venv/bin/python -I .agents/labs/cm_drive.py
--help`` (``cm_lab/drive.py``; the runbook is ``cm-lab-round-1.md``)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cm_lab.drive import main

if __name__ == "__main__":
    sys.exit(main())
