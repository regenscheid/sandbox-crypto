# SPDX-License-Identifier: MIT
"""Binding the agent runtime's sandbox in the policy middleware's store.

The policy middleware decides the requests of sandboxes bound to a stored policy
revision and refuses any other as ``sandbox_unbound``. The worker binds the research
sandboxes it creates (``Environments.revise``); the agent runtime's sandbox is the
launcher's, so the launcher asks the worker to bind it to its project's revision
(``POST /v1/runtime-sandboxes``) when the sandbox is Ready, and to unbind it when it is
deleted (``DELETE /v1/runtime-sandboxes/{id}``). The launcher never writes the store.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from .config import LauncherError
from .sandboxes import WorkerApi


def _warn(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


class WorkerBinding:
    """``runtime_owner.Binding`` through the worker's operator routes."""

    def __init__(self, api: WorkerApi, project: str, say: Callable[[str], None] = _warn) -> None:
        self.api = api
        self.project = project
        self.say = say

    async def bind(self, sandbox_id: str) -> None:
        try:
            revision = await self.api.bind_runtime(self.project, sandbox_id)
        except LauncherError as error:
            raise LauncherError(
                f"the runtime sandbox {sandbox_id} is not bound in the policy store: {error}; "
                "the policy middleware would refuse it as sandbox_unbound"
            ) from None
        self.say(f"bound runtime sandbox {sandbox_id} to {revision}")

    async def unbind(self, sandbox_id: str) -> None:
        try:
            await self.api.unbind_runtime(sandbox_id)
        except LauncherError as error:
            self.say(f"the binding of runtime sandbox {sandbox_id} remains: {error}")
