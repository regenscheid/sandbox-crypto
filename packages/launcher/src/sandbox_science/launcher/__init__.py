# SPDX-License-Identifier: MIT
"""The launcher: a local Sandbox Science from one config file.

It starts the parts as the commands they are (``sandbox-science-worker``,
``sandbox-science-harness``, ``sandbox-science-workspace``), never importing them, since
deployable members do not import each other. ``config.py`` reads the config file,
``state.py`` keeps what the launcher makes in its state directory (tokens, settings,
logs), and ``parts.py`` turns the config into each part's settings and command line.
``supervise.py`` runs the parts, ``instance.py`` keeps the running instance's lock and
record, and ``cli.py`` is ``sandbox-science up``, ``down`` and ``status``.
"""
