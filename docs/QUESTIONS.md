# Questions to the user

A main run can ask the user a question and wait for the answer: a preference or a research decision only the user can make, such as which of two approaches to take. This implements PD-23 to PD-26 and PD-41 of the [plans and delegation specification](specs/plans-and-delegation.md).

## The tool

A main run is offered `ask_user`. Its arguments:

- `question`: the question, 1 to 4,000 characters, with what the user needs to answer it.
- `options`: none, or two to four choices, each a `label` (at most 120 characters, distinct from the others) and an optional `detail` (at most 400) saying what it means.
- `multiple`: whether several options may be chosen; only with options.

The question, its labels and its details are the agent's text: line ends become line feeds, and a text holding a character the user may not see is refused (`check_agent_text`, threat model A3).

A run asks one question per reply, and at most 10 in all: a second question in the same reply is refused (`invalid_request`), and an eleventh with `policy_denied` (`limit_reached`). A replayed call is the question it asked, and counts once.

The call waits inside itself. The run keeps its session and makes no model call until the question closes, however long that takes: there is no default and no timeout (open question 6). Only that run waits: its jobs and delegated runs go on, and so do the project's other sessions (PD-26). The call polls the control store once a second.

The answer is the call's result, so it reaches the run and the call that asked, and two waiting calls never get each other's answers (PD-24). The result is JSON with the `question_id` and one of:

| `answer` | Means |
| --- | --- |
| `picked` | The user picked the options in `picked`, in the question's order. |
| `own_words` | The user answered in their own words, in `text`, kept as they wrote it. |
| `decide` | The user leaves it to the agent; a `note` asks it to decide and say what it chose and why. |
| `cancelled` | The user cancelled the question; a `note` asks the agent not to ask it again unless something changes. |
| `null` | The question closed without an answer; `state` and `note` say why. |

The tool's effect class is `read`: asking changes nothing a plan governs, so a run may ask while its plan is a draft. The run's record keeps the call's result and the question's ID, state and answer.

A run's limits are checked between model calls, so none ends the wait. A run that ends while it waits closes its question, as no one is left to read the answer: `cancelled` with the reason "the run was cancelled" when the user cancelled the run (RS-11), and `interrupted` with the reason "the run stopped before it was answered" when it stopped otherwise, as when the run service stops. A replayed call (the same operation ID) finds the question it asked and returns as soon as that question is closed.

A question is not a permission request (PD-25): an answer grants nothing, and the policy still decides every operation.

## Delegated runs

A delegated run is not offered `ask_user`, and a call to it is refused with `not_permitted_for_role` (PD-16). A check that needs the user stops with status `blocked` and its question in `open_questions` (PD-41).

## The store

`Questions` in [`questions.py`](../packages/orchestrator/src/sandbox_science/orchestrator/questions.py) keeps the questions in the control store's `user_questions` table (version 37), so they outlive the browser. Each method is one transaction.

- `ask` writes an open question for a run and its tool call. The same operation in the same session returns the question it asked; a different question under that operation is a conflict.
- `get` reads one question of a session; another session's is not found.
- `open` lists a session's open questions, oldest first, and `listed` its latest 50, open or closed.
- `answer` closes an open question with the user's answer. A pick must name one of the question's options, or several when `multiple` is set, and nothing else; an answer in the user's own words is 1 to 16,000 characters and is kept as written, without the check for hidden characters; `decide` and `cancel` carry nothing. A cancel closes the question `cancelled` with the reason "the user cancelled it"; any other answer closes it `answered`. The same answer again returns the question; another answer, or one to a closed question, is a conflict.
- `close` closes the open questions of a run, one question, or a whole session, `cancelled` or `interrupted`, with a reason, when no run can receive their answers.

A question is written once and closed once: the table's triggers refuse any other change, and any delete.

## Answering

The workspace answers through two routes ([workspace](WORKSPACE.md#api-v0)):

- `GET …/sessions/{session}/questions` lists the session's latest questions with the run that holds the session.
- `POST …/sessions/{session}/questions/{question}/answer` with `{"kind", "picked", "text"}` answers one: `picked` names the options, `free` carries the text, and `decide` and `cancel` carry nothing.

A chat message can also be the answer. While the run that holds the session (its lease is live) waits for an answer, a message the user writes to the session answers the oldest open question of that run, in the user's own words, instead of being queued (`RunState.post`, in one transaction). The question keeps the message's ID, and the message route's answer names the question instead of a message, so a retried write gets the same question back. A message longer than an answer may be (16,000 characters) is refused, naming the question. A question whose run no longer holds the session takes no message: the message is queued as any other, and the question closes when the service ends the run. This is a coordinator decision, which the owner can reverse: the message could instead wait in the queue for the run's next turn.

## When no run can receive the answer

- **The user cancels the run** (RS-11): the waiting call closes its question `cancelled`, "the run was cancelled".
- **The run stops otherwise**, as when the run service stops: the call closes it `interrupted`, "the run stopped before it was answered".
- **The run's process dies**: the run service ends the run when its lease expires (RS-12) and closes its open questions `interrupted`, "the run was interrupted".
- **The session closes** (`session close`): its open questions close `cancelled`, "the session was closed". A run still waiting reads that as its result.

## Not built yet

The chat panel that shows open questions with their age and takes the answers.
