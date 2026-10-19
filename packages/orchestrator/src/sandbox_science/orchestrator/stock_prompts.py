# SPDX-License-Identifier: MIT
"""Original, explicitly selected MAIN research framing; no tools or permissions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, cast

StockTemplateId = Literal["intake", "literature", "experiment", "proof-audit", "report"]
TEMPLATE_IDS: tuple[StockTemplateId, ...] = (
    "intake",
    "literature",
    "experiment",
    "proof-audit",
    "report",
)
MAX_TEMPLATE_BYTES = 2 * 1024


@dataclass(frozen=True, slots=True)
class StockPrompt:
    """One installed bounded template; selection changes framing, never capability."""

    template_id: StockTemplateId
    version: str
    text: str

    def validate(self) -> None:
        """Refuse invalid installed content before any selected model invocation."""
        if self.template_id not in TEMPLATE_IDS or not isinstance(cast(object, self.version), str):
            raise ValueError("invalid installed research template identity")
        if (
            re.fullmatch(rf"sandbox-stock-{self.template_id}-[1-9][0-9]{{0,8}}", self.version)
            is None
        ):
            raise ValueError("invalid installed research template version")
        if (
            not isinstance(cast(object, self.text), str)
            or not self.text
            or len(self.text) > MAX_TEMPLATE_BYTES
        ):
            raise ValueError("installed research template exceeds its text bounds")
        try:
            size = len(self.text.encode("utf-8"))
        except UnicodeError:
            raise ValueError("installed research template is not UTF-8") from None
        if size > MAX_TEMPLATE_BYTES:
            raise ValueError("installed research template exceeds its text bounds")


_CATALOG = MappingProxyType(
    {
        "intake": StockPrompt(
            "intake",
            "sandbox-stock-intake-2",
            "Research framing: intake. Establish the requested goal, the inputs already known "
            "and the evidence that would make the result useful. Separate supplied facts from "
            "assumptions and feasibility limits. Choose a small concrete next result under the "
            "existing authorization; ask for a missing decision only when it blocks progress. "
            "For explicitly extended exploration, that result is a first iteration: record "
            "hypotheses, relevant baseline evidence and stopping criteria, then reassess "
            "follow-on branches as results arrive. Bounded intake remains a bounded task. "
            "Use durable planning only when its tools are offered. Disclose missing capabilities "
            "without inventing tools, approvals, sources or results.",
        ),
        "literature": StockPrompt(
            "literature",
            "sandbox-stock-literature-2",
            "Research framing: literature synthesis. Inspect sources through the capabilities "
            "actually offered before citing their contents. Compare assumptions, methods, "
            "evidence and limitations, keeping each claim tied to the source actually read. "
            "Distinguish source claims from your deductions and unresolved disagreements. "
            "State each search's source, query and date; a search with no hits bounds only "
            "that search, and is no evidence that the work or attack does not exist. "
            "If retrieval or a necessary source is unavailable, state that limit and retain "
            "useful analysis of the available material; do not manufacture citations or access.",
        ),
        "experiment": StockPrompt(
            "experiment",
            "sandbox-stock-experiment-2",
            "Research framing: experiment. State the hypothesis, inputs, controls, stopping "
            "condition and expected evidence before the relevant experiment. Execute only "
            "through offered research tools under current policy. Preserve the actual "
            "environment, commands and observed outputs, including negative or partial results. "
            "Separate an intended experiment from one that ran, and a successful process from "
            "a supported conclusion. Reconcile uncertain effects before repeating them. "
            "For explicitly extended research, use results to reassess hypotheses and choose "
            "justified follow-on experiments; a pilot's stopping condition does not finish "
            "the investigation. Establish a faithful reference bridge before promoting a "
            "toy result to the target version and parameters. Respect bounded briefs and "
            "existing ceilings rather than extending work to spend the budget.",
        ),
        "proof-audit": StockPrompt(
            "proof-audit",
            "sandbox-stock-proof-audit-1",
            "Research framing: proof audit. Identify the precise claim and assumptions, then "
            "check the dependencies between its deductive steps. Distinguish a proof, a finite "
            "computation, an example and a heuristic. Use only available tools for checks and "
            "preserve their exact scope. Explain gaps without presenting a missing argument "
            "as a counterexample, or a bounded search as a general theorem. State which parts "
            "are established and which still need reasoning or evidence.",
        ),
        "report": StockPrompt(
            "report",
            "sandbox-stock-report-2",
            "Research framing: report. Organize the result around the requested question and "
            "connect material claims to inspected sources or exact observed outputs. Distinguish "
            "completed work, partial evidence, assumptions and remaining work. Link retained "
            "artifacts only when they exist and are accessible through the offered capabilities. "
            "Keep scientific conclusions separate from workflow completion, process exit "
            "status and author assertions; report uncertainty without inventing validation. "
            "For explicitly extended research, an interim report or finished checklist is "
            "not closure: assess remaining branches against the requested evidence and "
            "stopping criteria, and continue justified work within authorization. A bounded "
            "report request may finish without starting a new research program.",
        ),
    }
)


def select_template(template_id: str) -> StockPrompt:
    """Resolve a trusted exact selection; task text never selects a catalog entry."""
    if not isinstance(cast(object, template_id), str) or template_id not in TEMPLATE_IDS:
        raise ValueError("unknown research template")
    prompt = _CATALOG[template_id]
    prompt.validate()
    if prompt.template_id != template_id:
        raise ValueError("installed research template selection mismatch")
    return prompt
