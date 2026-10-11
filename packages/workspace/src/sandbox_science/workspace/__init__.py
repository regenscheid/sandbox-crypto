# SPDX-License-Identifier: MIT
"""The research workspace's web server (W4, D80; docs/WORKSPACE.md).

It serves the GUI, a Svelte app built into static files (``web/workspace``), and a
read-only JSON API over the harness's state directory. Until the control application
has an API of its own (W5), it reads the harness's records through the orchestrator's
``ProjectStore``.
"""
