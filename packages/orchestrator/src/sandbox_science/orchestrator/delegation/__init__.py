# SPDX-License-Identifier: MIT
"""Delegation: a run starts delegated runs, which run as a role of the same tool loop
(docs/specs/plans-and-delegation.md, PD-11 to PD-22 and PD-32 to PD-43).

- ``records``: delegations and each project's delegation setting, in the control store;
- ``specs``: the delegate tools as the model sees them;
- ``prompt``: a delegated run's system prompt and its first message, the brief;
- ``hook``: a parent run's delegations: its tools, its children and their handbacks.

The harness starts each child (``Harness.run_child``); this package does not import it.
"""
