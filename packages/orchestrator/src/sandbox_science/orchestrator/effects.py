# SPDX-License-Identifier: MIT
"""A shared stop signal for tool calls whose external effect is not known."""


class UnknownEffectError(Exception):
    """Keep the durable tool intent pending until its original effect is reconciled.

    Adapters raise this only when the operation may have been accepted but its
    result cannot be established. A definite refusal remains a normal tool error
    that the agent can read and correct.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
