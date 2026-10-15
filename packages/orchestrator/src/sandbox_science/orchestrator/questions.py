# SPDX-License-Identifier: MIT
"""Questions a main run asks the user, and their answers (plans-and-delegation spec,
PD-23 to PD-26).

A question is the agent's text, optionally with two to four options, and whether
several may be chosen. It is written once, ``open``, and stays in the control store, so
it outlives the browser. The user closes it with an answer: options picked, an answer
in their own words, the agent to decide, or a cancel. A question also closes when its
run cannot receive the answer: ``cancelled`` with the run or the session,
``interrupted`` when the service restarted under it. Once closed, nothing changes it.

A question is not a permission request (PD-25): it records a preference or a research
decision, while a permission request decides an operation under the policy, and they
are kept apart. The question's text and its options are the agent's, so they hold no
character the user may not see (``check_agent_text``, threat model A3); the answer is
the user's and is kept as written. Each method is one transaction and may run in a
thread.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Final, Literal, cast

from pydantic import Field, ValidationError

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ExecutionContext, new_id
from sandbox_science.contracts.model import Contract

from .control import ControlError, ControlStore
from .control.errors import CONFLICT, INVALID_REQUEST, NOT_FOUND
from .documents import DocumentStore
from .documents.agent_text import agent_text

MAX_QUESTION: Final = 4000
"""Characters of a question's text."""
MAX_LABEL: Final = 120
MAX_DETAIL: Final = 400
MAX_ANSWER: Final = 16_000
"""Characters of an answer in the user's own words."""

QuestionState = Literal["open", "answered", "cancelled", "interrupted"]
AnswerKind = Literal["picked", "free", "decide", "cancel"]
"""The user picked options, answered in their own words, asked the agent to decide, or
cancelled the question (PD-23)."""


class QuestionOption(Contract):
    """One choice a question offers: a short label, and what it means."""

    label: Annotated[str, Field(min_length=1, max_length=MAX_LABEL)]
    detail: Annotated[str, Field(min_length=1, max_length=MAX_DETAIL)] | None = None


class Answer(Contract):
    """How the user closed a question."""

    kind: AnswerKind
    picked: tuple[str, ...] = ()
    """The labels of the options picked, in the question's order."""
    text: str | None = None
    """An answer in the user's own words."""


class Question(Contract):
    """A question as the store keeps it."""

    question_id: str
    project: str
    session: str
    run: str
    """The run that asked, by the name its record is kept under."""
    tool_call: str
    text: str
    options: tuple[QuestionOption, ...]
    multiple: bool
    state: QuestionState
    asked_at: datetime
    answer: Answer | None = None
    closed_at: datetime | None = None
    reason: str | None = None
    """Why a question closed without an answer: the run was cancelled, the session
    closed, or the service restarted."""
    message: str | None = None
    """The chat message that answered it, when the user answered by writing to the
    session."""


_COLUMNS: Final = (
    "question_id, project_id, session_id, run, tool_call, text, options, multiple, state, "
    "asked_at, answer, closed_at, reason, message_id"
)
INTERRUPTED: Final = "the run was interrupted"
"""Why the run service closes the questions of a run whose holder is gone."""
SESSION_CLOSED: Final = "the session was closed"
LISTED: Final = 50
"""The most questions a listing returns: a session's latest."""


def _question(row: tuple[object, ...]) -> Question:
    (
        question_id, project, session, run, tool_call, text, options, multiple, state,
        asked_at, answer, closed_at, reason, message,
    ) = row  # fmt: skip
    return Question(
        question_id=str(question_id),
        project=str(project),
        session=str(session),
        run=str(run),
        tool_call=str(tool_call),
        text=str(text),
        options=tuple(QuestionOption.model_validate(o) for o in json.loads(str(options))),
        multiple=bool(multiple),
        state=cast(QuestionState, state),
        asked_at=datetime.fromisoformat(str(asked_at)),
        answer=None if answer is None else Answer.model_validate_json(str(answer)),
        closed_at=None if closed_at is None else datetime.fromisoformat(str(closed_at)),
        reason=None if reason is None else str(reason),
        message=None if message is None else str(message),
    )


class Questions:
    """The questions in one control store, which its owner closes: the store itself, or
    the one a document store opened."""

    def __init__(self, store: ControlStore | DocumentStore, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.control = store if isinstance(store, ControlStore) else store.control
        self.clock = clock

    def _now(self) -> str:
        return self.clock.now().astimezone(UTC).isoformat()

    def ask(
        self,
        context: ExecutionContext,
        run: str,
        tool_call: str,
        operation: str,
        text: str,
        options: Sequence[object] = (),
        *,
        multiple: bool = False,
    ) -> Question:
        """A new open question, or the one this operation asked already. Its text and
        options are the agent's: kept with line feeds for line ends, and refused when
        they hold a character the user may not see (threat model, A3)."""
        try:
            offered = tuple(QuestionOption.model_validate(option) for option in options)
        except ValidationError as error:
            first = error.errors()[0]
            where = ".".join(str(part) for part in first["loc"])
            raise ControlError(INVALID_REQUEST, f"an option's {where}: {first['msg']}") from None
        kept = agent_text(text.strip(), "the question")
        if not 1 <= len(kept) <= MAX_QUESTION:
            raise ControlError(INVALID_REQUEST, f"a question is 1 to {MAX_QUESTION} characters")
        if offered and not 2 <= len(offered) <= 4:
            raise ControlError(INVALID_REQUEST, "a question has two to four options, or none")
        if multiple and not offered:
            raise ControlError(
                INVALID_REQUEST, "only a question with options lets several be chosen"
            )
        cleaned = tuple(
            QuestionOption(
                label=agent_text(option.label.strip(), f"option {number}'s label"),
                detail=None
                if option.detail is None
                else agent_text(option.detail, f"option {number}'s detail"),
            )
            for number, option in enumerate(offered, 1)
        )
        labels = [option.label for option in cleaned]
        if any(not label for label in labels) or len(set(labels)) != len(labels):
            raise ControlError(INVALID_REQUEST, "each option has a label of its own")
        encoded = json.dumps([o.model_dump(exclude_none=True) for o in cleaned])
        with self.control.write() as tx:
            db = tx.sql
            found = db.execute(
                f"SELECT {_COLUMNS} FROM user_questions "  # noqa: S608 - fixed columns
                "WHERE project_id = ? AND session_id = ? AND operation = ?",
                (context.project_id, context.session_id, operation),
            ).fetchone()
            if found is not None:
                question = _question(found)
                if (question.text, question.options, question.multiple) != (
                    kept,
                    cleaned,
                    multiple,
                ):
                    raise ControlError(CONFLICT, f"operation {operation!r} asked another question")
                return question
            question_id = new_id("qst")
            try:
                db.execute(
                    "INSERT INTO user_questions (question_id, project_id, session_id, run, "
                    "tool_call, operation, text, options, multiple, state, asked_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                    (
                        question_id,
                        context.project_id,
                        context.session_id,
                        run,
                        tool_call,
                        operation,
                        kept,
                        encoded,
                        int(multiple),
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ControlError(INVALID_REQUEST, f"the question is refused: {error}") from None
            return _get(db, context.project_id, context.session_id, question_id)

    def get(self, project: str, session: str, question_id: str) -> Question:
        with self.control.read() as tx:
            return _get(tx.sql, project, session, question_id)

    def open(self, project: str, session: str) -> list[Question]:
        """The session's open questions, oldest first."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                f"SELECT {_COLUMNS} FROM user_questions "  # noqa: S608 - fixed columns
                "WHERE project_id = ? AND session_id = ? AND state = 'open' ORDER BY rowid",
                (project, session),
            ).fetchall()
        return [_question(row) for row in rows]

    def listed(self, project: str, session: str) -> list[Question]:
        """The session's latest questions, open or closed, oldest first."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                f"SELECT {_COLUMNS} FROM user_questions "  # noqa: S608 - fixed columns
                "WHERE project_id = ? AND session_id = ? ORDER BY rowid DESC LIMIT ?",
                (project, session, LISTED),
            ).fetchall()
        return [_question(row) for row in reversed(rows)]

    def answer(self, project: str, session: str, question_id: str, answer: Answer) -> Question:
        """Close an open question with the user's answer. The same answer again is the
        answer already given; another one, or one to a closed question, a conflict."""
        with self.control.write() as tx:
            db = tx.sql
            question = _get(db, project, session, question_id)
            given = _checked(question, answer)
            if question.state != "open":
                if question.answer == given:
                    return question
                raise ControlError(
                    CONFLICT,
                    f"question {question_id} is {question.state}, not open",
                    {"state": question.state},
                )
            state = "cancelled" if given.kind == "cancel" else "answered"
            db.execute(
                "UPDATE user_questions SET state = ?, answer = ?, closed_at = ?, reason = ? "
                "WHERE question_id = ? AND state = 'open'",
                (
                    state,
                    given.model_dump_json(exclude_defaults=True),
                    self._now(),
                    "the user cancelled it" if given.kind == "cancel" else None,
                    question_id,
                ),
            )
            return _get(db, project, session, question_id)

    def close(
        self,
        project: str,
        session: str,
        *,
        run: str | None = None,
        question: str | None = None,
        state: Literal["cancelled", "interrupted"],
        reason: str,
    ) -> list[str]:
        """Close the open questions of ``run``, the ``question`` alone, or those of the
        whole session, when no run can receive their answers; the IDs closed."""
        with self.control.write() as tx:
            return close_open(
                tx.sql,
                project,
                session,
                self._now(),
                run=run,
                question=question,
                state=state,
                reason=reason,
            )


def close_open(
    db: sqlite3.Connection,
    project: str,
    session: str,
    at: str,
    *,
    run: str | None = None,
    question: str | None = None,
    state: Literal["cancelled", "interrupted"],
    reason: str,
) -> list[str]:
    """``Questions.close`` in the open change ``db``, at the time ``at``."""
    where = "project_id = ? AND session_id = ? AND state = 'open'"
    values: list[object] = [project, session]
    if run is not None:
        where += " AND run = ?"
        values.append(run)
    if question is not None:
        where += " AND question_id = ?"
        values.append(question)
    ids = [
        str(row[0])
        for row in db.execute(
            f"SELECT question_id FROM user_questions WHERE {where}",  # noqa: S608
            values,
        )
    ]
    db.execute(
        f"UPDATE user_questions SET state = ?, closed_at = ?, reason = ? WHERE {where}",  # noqa: S608
        [state, at, reason, *values],
    )
    return ids


def answer_by_message(
    db: sqlite3.Connection,
    project: str,
    session: str,
    message_id: str,
    text: str,
    at: str,
    *,
    live_after: float,
) -> Question | None:
    """The question a chat message answers, in the open change ``db``: the one it
    answered already, or else the oldest open question of the run that holds the session
    (its lease renewed after ``live_after``), which it answers now in the user's own
    words. ``None`` when no run that holds the session waits for an answer: the message
    is then an ordinary one."""
    row = db.execute(
        f"SELECT {_COLUMNS} FROM user_questions WHERE message_id = ?",  # noqa: S608
        (message_id,),
    ).fetchone()
    if row is not None:
        found = _question(row)
        if (found.project, found.session) != (project, session) or (
            found.answer is None or found.answer.text != text.strip()
        ):
            raise ControlError(CONFLICT, f"message {message_id!r} was written with other content")
        return found
    row = db.execute(
        f"SELECT {_COLUMNS} FROM user_questions WHERE question_id = ("  # noqa: S608
        "SELECT q.question_id FROM user_questions AS q JOIN run_leases AS l "
        "ON l.project_id = q.project_id AND l.session_id = q.session_id AND l.run = q.run "
        "WHERE q.project_id = ? AND q.session_id = ? AND q.state = 'open' "
        "AND l.renewed_at > ? ORDER BY q.rowid LIMIT 1)",
        (project, session, live_after),
    ).fetchone()
    if row is None:
        return None
    question = _question(row)
    try:
        given = _checked(question, Answer(kind="free", text=text))
    except ControlError as error:
        raise ControlError(
            error.kind,
            f"the message answers question {question.question_id}: {error.message}",
            {"question": question.question_id},
        ) from None
    db.execute(
        "UPDATE user_questions SET state = 'answered', answer = ?, closed_at = ?, "
        "message_id = ? WHERE question_id = ? AND state = 'open'",
        (given.model_dump_json(exclude_defaults=True), at, message_id, question.question_id),
    )
    return _get(db, project, session, question.question_id)


def _get(db: sqlite3.Connection, project: str, session: str, question_id: str) -> Question:
    row = db.execute(
        f"SELECT {_COLUMNS} FROM user_questions "  # noqa: S608 - fixed columns
        "WHERE project_id = ? AND session_id = ? AND question_id = ?",
        (project, session, question_id),
    ).fetchone()
    if row is None:
        raise ControlError(NOT_FOUND, f"no question {question_id} in this session")
    return _question(row)


def _checked(question: Question, answer: Answer) -> Answer:
    """The answer as kept, or a refusal when it does not fit the question."""
    labels = [option.label for option in question.options]
    if answer.kind == "picked":
        picked = [label for label in labels if label in answer.picked]
        if (
            not question.options
            or not answer.picked
            or len(picked) != len(answer.picked)
            or (not question.multiple and len(picked) != 1)
            or answer.text is not None
        ):
            raise ControlError(
                INVALID_REQUEST,
                "pick one of the question's options"
                + (", or several" if question.multiple else ""),
            )
        return Answer(kind="picked", picked=tuple(picked))
    if answer.picked:
        raise ControlError(INVALID_REQUEST, "only a pick names options")
    if answer.kind == "free":
        text = (answer.text or "").strip()
        if not text or len(text) > MAX_ANSWER:
            raise ControlError(
                INVALID_REQUEST, f"an answer in your own words is 1 to {MAX_ANSWER} characters"
            )
        return Answer(kind="free", text=text)
    if answer.text is not None:
        raise ControlError(INVALID_REQUEST, f"a {answer.kind} has no text")
    return Answer(kind=answer.kind)
