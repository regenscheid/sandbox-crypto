# SPDX-License-Identifier: MIT
"""The control store: the connection, transactions, schema and errors its repositories share.

The repositories (documents, comments, reviews, findings, plans, artifacts, math checks)
keep their own SQL and run it in the ``Tx`` that ``ControlStore.read()`` or
``ControlStore.write()`` gives them. The schema of all of them is one ordered list of
migrations (``migrations.py``).
"""

from .errors import ControlError
from .store import DEFAULT_TIMEOUT, ControlStore, Tx

__all__ = ["DEFAULT_TIMEOUT", "ControlError", "ControlStore", "Tx"]
