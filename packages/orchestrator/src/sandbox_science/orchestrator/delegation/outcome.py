# SPDX-License-Identifier: MIT
"""A delegated run's result: its final answer, checked against its output schema
(plans-and-delegation spec, PD-17 to PD-19, PD-41).

A check's final answer is a JSON object (``CheckResult``). The ``Outcome`` hook reads
each closing answer: a valid one is the result; an invalid one gets one correction, a
turn with the errors (PD-18). If the correction does not validate either, or the run has
no turn left, the answer stands and the result is missing, with the errors; nothing is
filled in. The answer itself is kept unchanged as the raw payload.
"""

from __future__ import annotations

import re
from typing import Annotated, Final, Literal, Self

from pydantic import Field, JsonValue, ValidationError, model_validator

from sandbox_science.contracts.model import StrictContract
from sandbox_science.orchestrator.hooks import Closing, Notice, RunView, TurnHook
from sandbox_science.orchestrator.notices import OUTCOME_CORRECTION

FORMAT: Final = "sandbox-check-result-1"
MAX_ERRORS: Final = 5
MAX_BYTES: Final = 32768
"""The longest result, as UTF-8 JSON."""
KEPT_ERRORS: Final = 4
_FENCED: Final = re.compile(r"^```(?:json)?\s*\n(.*)\n```$", re.DOTALL)
_ITEMS = Field(default=(), max_length=20)
Item = Annotated[str, Field(min_length=1, max_length=2000)]

SHAPE: Final[dict[str, JsonValue]] = {
    "status": "complete | partial | blocked",
    "verdict": "confirmed | refuted | inconclusive",
    "summary": "what you checked, how, and what you found (at most 8000 characters)",
    "obstacle": "when blocked: what prevents the check, such as a missing capability",
    "deviations": ["where you departed from the brief, and why"],
    "open_questions": ["what you could not settle, such as a question for the user"],
}
"""The result's fields, as the correction notice describes them."""


class CheckResult(StrictContract):
    """What a check returns (``sandbox-check-result-1``). Its fields are the delegated
    run's claims; the outcome's ``ended`` is the platform's observation."""

    status: Literal["complete", "partial", "blocked"]
    verdict: Literal["confirmed", "refuted", "inconclusive"]
    summary: str = Field(min_length=1, max_length=8000)
    obstacle: str | None = Field(default=None, min_length=1, max_length=2000)
    deviations: tuple[Item, ...] = _ITEMS
    open_questions: tuple[Item, ...] = _ITEMS

    @model_validator(mode="after")
    def _blocked_names_its_obstacle(self) -> Self:
        if (self.status == "blocked") != (self.obstacle is not None):
            raise ValueError("a blocked check names its obstacle, and only a blocked one does")
        return self


def parse(answer: str | None) -> CheckResult:
    """The result an answer gives: a JSON object, alone or in one fenced block. Raises
    ``ValueError`` with what is wrong."""
    text = (answer or "").strip()
    if (fenced := _FENCED.match(text)) is not None:
        text = fenced[1]
    try:
        result = CheckResult.model_validate_json(text)
    except ValidationError as error:
        found = [
            f"{'.'.join(str(part) for part in e['loc']) or 'result'}: {e['msg']}"
            for e in error.errors()[:MAX_ERRORS]
        ]
        raise ValueError("; ".join(found)) from None
    if len(result.model_dump_json().encode()) > MAX_BYTES:
        raise ValueError(f"the result is longer than {MAX_BYTES} bytes")
    return result


class Outcome(TurnHook):
    """A delegated run's result, read from its closing answers."""

    name = "outcome"

    def __init__(self) -> None:
        self.result: CheckResult | None = None
        self.errors: list[str] = []
        self.corrected = False

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        try:
            self.result = parse(answer)
        except ValueError as error:
            self.result = None
            self.errors = [*self.errors, str(error)[:1000]][-KEPT_ERRORS:]
            if not self.corrected:
                # One correction (PD-18); with no turn or room left, the answer stands.
                self.corrected = True
                found = {"errors": self.errors[-1:], "format": FORMAT, "fields": SHAPE}
                return Closing("continue", (Notice(self.name, (), OUTCOME_CORRECTION.text, found),))
        return Closing()

    def summary(self) -> object | None:
        return {
            "format": FORMAT,
            "result": self.result.model_dump(mode="json") if self.result is not None else None,
            "errors": list(self.errors),
        }
