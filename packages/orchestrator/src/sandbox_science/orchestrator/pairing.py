# SPDX-License-Identifier: Apache-2.0
"""Calls and their outputs, paired before a request (docs/specs/context-management.md,
CM-29a): a call without an output, as an interrupted run leaves, gets a synthetic output
saying it was aborted, right after the call; then an output without its call is left
out. Together they keep every call and output in a pair.

A port of ``ensure_call_outputs_present`` and ``remove_orphan_outputs``, which
``normalize_history`` runs in this order, from OpenAI Codex's
``codex-rs/core/src/context_manager/normalize.rs`` (openai/codex, ``rust-v0.163.0`` at
``6b99197``), on the Responses API's input items as JSON objects. Changed from the
original: only function calls and their outputs, the kinds this platform sends; the
synthetic output has no item ID, which a request does not need, and the same text each
time, which keeps the request's prefix stable; and an output without its call is
logged, where Codex's debug builds panic.
"""

# The original's notices:
#
# OpenAI Codex
# Copyright 2025 OpenAI
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this
# file except in compliance with the License. You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under
# the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific language
# governing permissions and limitations under the License.

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Final

_log = logging.getLogger(__name__)

Item = dict[str, Any]

CALL: Final = "function_call"
OUTPUT: Final = "function_call_output"
ABORTED: Final = "aborted"
"""The output a call without one is given, Codex's."""


def paired(items: Sequence[Item]) -> list[Item]:
    """``items`` with every call answered and every output's call present."""
    return _without_orphan_outputs(_with_outputs(items))


def _with_outputs(items: Sequence[Item]) -> list[Item]:
    answered = {item.get("call_id") for item in items if item.get("type") == OUTPUT}
    found: list[Item] = []
    for item in items:
        found.append(item)
        if item.get("type") == CALL and (call := item.get("call_id")) not in answered:
            _log.info("the call %s has no output; it is sent as aborted", call)
            found.append({"type": OUTPUT, "call_id": call, "output": ABORTED})
    return found


def _without_orphan_outputs(items: Sequence[Item]) -> list[Item]:
    calls = {item.get("call_id") for item in items if item.get("type") == CALL}
    kept: list[Item] = []
    for item in items:
        if item.get("type") == OUTPUT and (call := item.get("call_id")) not in calls:
            _log.warning("the output of %s has no call; it is left out", call)
            continue
        kept.append(item)
    return kept
