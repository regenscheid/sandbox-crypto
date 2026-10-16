# Context management for long research sessions

Date: 2026-10-15. **Status: Draft, for the owner's review. Nothing in it is built.** The owner decided on 2026-10-15: the approach works for both Claude and GPT models, and a session may switch between them; the project follows proven designs, Claude Science's and OpenScience's, and adopts no context-management library, since none is known to be good for this work; the working context's ceiling is a per-role setting, 600,000 tokens by default, never more than the model's window less its maximum output, and the first lab round checks what that default does to quality; the spec is organized by layers, with the research state kept explicitly; and continuity probes on both model families are the first build's acceptance condition. Everything else is recommended and waits for the owner's review, the open questions first. W0 behavior specification; conventions in the [index](README.md).

## Scope

A research session runs for days. Its runs together read and produce millions of tokens: papers, tool output, computations and decisions. A model reads far less in one call, and reasons best over context that is curated. This spec says how the platform keeps everything, what each model call sees, how older work is folded into shorter records, how the research state is kept outside the conversation, and how the agent gets exact values back.

**Today.** A run is one user turn of at most 30 model calls, and each call sends the whole conversation. Past 400,000 characters (about 100,000 tokens), or on the gateway's context error, the run stops with `context_limit`. A new run sees only the earlier runs' tasks, user messages and final answers, within 100,000 characters; their tool calls and results are dropped ([run service](run-service.md), RS-10). So on day 3 the agent sees none of the evidence it gathered on day 1, unless it was written into an answer, the ledger, a document or memory.

**The design.** It follows Claude Science's rolling folds (CScience): keep everything in an archive, show the model a bounded view, fold the oldest work in chunks, and let the agent get exact values back from the archive. It changes four things:

1. Code carries state forward at every fold: the user's messages word for word, and a digest of the research state, from the ledger and the plan. Claude Science keeps such carries only in its fallback path.
2. The view spans the whole session, not one run. It replaces RS-10's earlier conversation.
3. Code checks that the values and search keys in a fold occur in the span it covers.
4. Two native model adapters: Claude through the gateway's `/v1/messages`, with the rules that bind thinking to its history, and GPT through `/v1/responses`, with reasoning items.

The spec has six layers: the archive, the research state, folds, the working view, retrieval and the model adapters.

**The ceiling.** 600,000 tokens is the owner's default for how much live context a call may carry, so that the model works with several hundred thousand tokens of recent work. It is a setting, not a finding: studies of long contexts find that models reason less well as the context grows (Chroma's "Context Rot"; the Graphwalks results at 256,000 and 1,000,000 tokens), so more live context is not always better. The first lab round checks the default's effect on quality, and the owner lowers it if the larger context does worse (CM-27).

**Out of scope.** Project memory, which stays as it is ([project memory](project-memory.md)). The consistency reviewer's and the job watcher's inputs, which are bounded on their own. Provider-native compaction (open question 3). A session scratchpad, which comes only if the continuity probes find state that neither the ledger nor the plan holds.

Terms:

- **Archive**: every original message of a session, kept for as long as the project exists.
- **View**: what one model call receives.
- **Ceiling** (C): the most tokens a call's input may hold.
- **Raw tail**: the newest messages, shown word for word.
- **Fold**: the replacement, in the view, of the oldest part of the raw tail by a fold record. Its **span** is the run of archive messages it covers.
- **Fold record**: a span header, a skeleton and marker lines built by code, a narrative written by a model, and search keys. A first-level fold covers raw messages; an **overview** covers earlier fold records.
- **State block**: one message, rebuilt by code, holding the user's own words and the research-state digest.
- **Research state**: where the research stands, kept in the ledger, the session plan and the read register.
- **Rebuild**: a moment when code recomposes the view: a fold, and a run's start.
- **Safe point**: a moment in a run when no tool call waits for its result: before a model call, and while the run waits on jobs, delegations or a question.

## Behavior

### General

- CM-1. **Which runs.** Main runs get the session view (CM-29). Delegated runs fold within their own run (CM-37). The consistency reviewer and the job watcher keep their own bounded inputs and have no view.
- CM-2. **Both model families** (owner, 2026-10-15). The archive, the research state, fold records, the state block and the retrieval tools are plain text and records that any model reads. A session may switch between Claude and GPT models. Only native items, such as thinking and reasoning, stay with the model that made them (CM-42).
- CM-2a. **One replaceable subsystem** (new). The view builder, the fold engine (scheduling, skeletons, enrichment, validation, overviews and marker retention) and the retrieval tools sit behind one interface. The loop calls it at its safe points: it hands over each appended message and each call's usage, and asks for the next call's view. The interface lives outside the loop's own module, as a turn hook does (refactoring plan, working rule 7). The archive and the research state are stable stores that the subsystem reads and does not own. Replacing the subsystem, with a plain summary, a provider's compaction or a library, changes neither store, nor the loop, nor the model adapters.

### Layer 1: the archive

- CM-3. **What it holds.** Every message of every run of the session, delegated runs included: tasks, the user's messages and the comments addressed to the session (RS-6b), notices, the assistant's text and tool calls, tool results, delegation outcomes as the parent received them, and the state blocks and fold records the platform sent. Each message has a stable ID (`msg-…`), its position in the session (an integer that only grows), its run, its role and kind, and its time; a tool result also has its call ID and tool (new).
- CM-4. **Written as the run goes.** Each message is appended before the next model call, as run events are (RS-13), but whole: unlike an event, it is never cut. An interrupted run keeps every message it appended. Nothing is changed or deleted while the project exists (CScience keeps a whole-session log; Codex keeps its rollout file).
- CM-5. **Native payloads.** Beside each assistant message's provider-neutral copy, the archive keeps the provider's native response byte for byte: Claude's content blocks, including thinking blocks with empty visible text, their signatures, and redacted thinking; GPT's output items, with encrypted reasoning, `phase` and the item IDs as the gateway gave them. It also records the provider model and the gateway's version (CM-39).
- CM-6. **Files and outcomes.** A tool result links to the files its call saved: the whole output behind a sample ([tools](tools.md), Shell 5) and the artifact revisions the call recorded ([research artifacts](research-artifacts.md), RA-27). A delegation outcome links to its delegation (PD-17).
- CM-7. **The search index.** The archive keeps a full-text index of messages' text, tool calls (names and arguments), tool results, notices, delegation outcomes, and the saved output files behind samples, up to 1 MiB each. It matches words, quoted phrases and substrings, so part of a long number or a hash finds it. It knows its coverage: the positions it indexed, and the files it left out, with the reason. The index is SQLite FTS5, which is part of the standard library; CScience indexes its whole log the same way.

### Layer 2: the research state

- CM-8. **What it is.** An explicitly kept record of where the research stands, not a by-product of summaries. It lives in the [ledger](ledger.md), the session plan ([plans and delegation](plans-and-delegation.md)) and the read register (CM-12):

  | Item | Kept as | New here |
  | --- | --- | --- |
  | Open questions | An `issue` of kind `question`, `open` | The issue's kind |
  | Active hypotheses | An `issue` of kind `hypothesis`, `open`, resolved by a claim that `answers` it | The kind |
  | Assumptions, and what depends on them | An `assumption`, the `assumes` links to it, and the platform's flags (LG-27, LG-28) | Nothing |
  | Decisions, including rejected and reversed ones, with their reasons and the conditions that would justify reopening them | A `decision`, its status, the reason of the revision that gave it that status, and its reopen condition | The reopen condition and its links (CM-10) |
  | Unresolved contradictions between sources or results | An `issue` of kind `contradiction`, `open`, which `concerns` the entries that disagree | The kind; it concerns at least two entries |
  | Findings, with evidence type, scope and caveats | A `claim` or `result`, its evidence links' types and scope notes (LG-8, LG-9), and its caveats | The caveats |
  | Attempts, and how they ended | Work records (LG-6): `limit`, `obstruction_claimed`, `produced`, `assessment` | Nothing |
  | Which sources were read completely, and which only in part | The read register (CM-12), shown on `reference` entries | All of it |
  | Where the session's work stands | The session plan: its steps, their statuses and notes; a step may name the ledger task it advances (LG-40) | Nothing |

- CM-9. **Provenance** (new). Every entry revision and work record an agent writes keeps its basis and its anchors:
  - the basis: `user` (the user's decision or statement), `observed` (it restates what the platform observed: a tool result, an artifact, a line of a PDF) or `interpreted` (the agent's reading or inference). A change that names none is `interpreted`;
  - the anchors: at least one place the item comes from, besides the change's own tool call: an archive message (by its ID, or a tool result by its call ID), a PDF anchor (LG-8), an artifact revision or an entry revision.

  Code checks the basis against the anchors. `user` needs an anchor on a message the user wrote: a USER row, or a comment addressed to the session. `observed` needs an anchor on a tool result, an artifact revision or a PDF anchor. An agent's change that creates an entry, changes its statement, fields or caveats, or moves a decision to `in_effect`, `rejected` or `reversed`, and every work record, names at least one anchor; a change of another status, a title or links alone needs only its reason. A change that fails these checks is refused with `provenance_required`, naming what is missing. The user's own changes have the basis `user` and need no anchor. Entries written before this change show their basis as not recorded. The basis and anchors are not evidence: the gate (LG-17) reads only evidence links.
- CM-10. **What the ledger gains** (new; listed again under [Changes to other specs](#changes-to-other-specs)):
  1. An issue has a kind: `question`, `hypothesis`, `idea`, `experiment`, `contradiction` or `reproduction` (a failed reproduction). Older issues read as `question`. A `contradiction` `concerns` at least two entries, findings or document blocks, and its statement says what disagrees, with an anchor on each side.
  2. A decision has a reopen condition: text saying what would justify deciding again, such as "reconsider index calculus if assumption Y becomes available", and optional `reopen_on` links to the entries the condition watches: an assumption, an issue, a claim, a result or a reference. A decision moved to `rejected` or `reversed` needs a reason; its reopen condition may say "none".
  3. When an entry that a `reopen_on` link names changes its status or its statement, the platform flags the decision for reconsideration, as LG-28 flags dependents. The flag changes nothing else (LG-29).
  4. A claim or result has caveats: up to 10 short lines, each at most 300 characters, that qualify it in ways its scope notes and assumptions do not, such as "heuristic: assumes the reduced basis behaves like a random one" or "estimated from 2^20 samples".
  5. Entry revisions and work records keep their basis and anchors (CM-9). An anchor on an archive message is a transcript anchor, the contract addition the index approved ([addition 3](README.md#proposed-contract-additions-new)).
- CM-11. **Failure is not impossibility.** The research state keeps three things apart, and the digest labels them differently (CM-16):
  - an attempt that did not succeed: a work record ending `limit`, which says what ran out (time, tools, context or ideas), or a claim of kind `observation` with `numerical_experiment` or `finite_test` evidence and its scope, such as "no collision in 2^40 trials";
  - a claimed obstruction or negative result that is not settled: a work record ending `obstruction_claimed` that no assessment has accepted, or a claim of kind `negative result` that is `proposed` or `disputed`, whatever evidence it holds. An agent can propose such a claim and attach its own proof before anyone checks it;
  - a settled impossibility, in the ledger's settled statuses only: an issue `obstructed` by a decision in effect that names its excluded scope and an accepted assessment (LG-7); a project claim of kind `negative result` that is `established`, which only a decision that passes the gate makes it (LG-17); or a published one that is `recorded` and confirmed as LG-16 says, labelled as its source's claim.

  Only the last is called impossible, always with its scope (the excluded scope, or the claim's statement and evidence scope notes) and the assumptions it rests on (its `assumes` links). A settled entry with an open flag (LG-28), such as one whose assumption changed, renders as unresolved, with the flag's cause, until the flag is cleared. A proposed, disputed or flagged negative result never renders as impossible: the digest says "claimed, not established", "disputed" or "flagged", with its handle. A failed numerical experiment never becomes an impossibility by being folded or summarized.
- CM-12. **The read register** (new). The platform records, from the tool records, how much of each source a session read. It is an observation, not the agent's statement. A source is a PDF artifact revision, an ePrint paper, a fetched web page or a saved output file. Its extent is one of:
  - `whole`: every page of a PDF returned whole by `pdf_pages`; every byte of a file read with `read_file`, counted as PD-18 counts a whole read; a fetch that returned the whole page;
  - `partial`: part of it, saying which, such as pages 3–7 of 21, a byte range, or the first part of a fetched page;
  - `abstract_only`: a catalog record or abstract, such as an ePrint entry's metadata;
  - `preview_only`: a sample or preview whose whole the session never read;
  - `unknown`: a tool that does not say what it returned, such as a connector tool the platform does not describe.

  A `pdf_search` hit reads lines, not pages. A `reference` entry shows the extents recorded for its PDF and its identifiers. The agent may add its own statement of how far it read a source the register cannot observe; that is kept as the agent's, with the basis `interpreted`. This settles the backlog's request that a reference record how much of its source was read (LG-15).
- CM-13. **Authority** (LG-13, LG-17; owner, 2026-10-15). Agents propose; only the user, or a decision that passes the gate, settles. The research state adds no authority: the user, the main agent and delegated agents keep LG-13's, and a fold's proposals have none of their own (CM-15).
- CM-14. **The agent keeps it current.** As it works, the agent records the research state with the ledger and plan tools. Its system prompt says when (CM-48): when it abandons an approach, chooses a method or a scope, gets a result, relies on something unproven, finds two sources or results that disagree, or fails at an attempt, and before it marks a plan step done.
- CM-15. **Folds propose updates** (new). Each fold's enrichment call (CM-19) also proposes research-state updates for its span, as typed operations:
  - a new issue: a question, a hypothesis or a contradiction that the span shows and the ledger lacks;
  - a decision that the span shows made but that nobody recorded: an approach abandoned, to be recorded `rejected` with its reason and reopen condition, or a method or scope chosen;
  - a work record on an existing issue, claim or result: what was tried, and how it ended;
  - a caveat on an existing claim or result;
  - an assumption the span relied on, with the entries that rely on it.

  Each proposal names its basis and its anchors, which lie in the span. Code validates each one: its form; that the handles it names exist; that its anchors lie in the span; that its numbers and identifiers occur in the span (CM-20); and that its text holds no hidden character (`check_agent_text`). It keeps the valid ones, at most 20 per fold, as the fold's proposals, recorded as agent-proposed. Nothing is written to the ledger. A proposal waits, shown in the digest, until the main agent or the user accepts it or dismisses it with a reason. The main agent accepts proposals with one tool call, as it imports a results file (LG-41), with its own authority: a status that needs a decision still needs one, and the gate still applies. The user accepts or dismisses them in the GUI. A fold's proposals change no entry and settle nothing (owner, 2026-10-15).
- CM-16. **The digest.** At every rebuild, code renders a digest of the research state into the state block (CM-26). Its sections, in order, each with its share of the digest's budget:
  1. the session plan: its goal and version, each step's status and note, and the current step;
  2. decisions: first the user's standing constraints, which are decisions in effect with the basis `user` (CM-48); then rejected and reversed decisions, which stop repeated work; then the others in effect, and proposed ones. Each with its handle, statement, status, reason, reopen condition and any open flag;
  3. open questions and hypotheses, and open contradictions with what they concern;
  4. active assumptions, each with the entries that rely on it, and their open flags;
  5. findings: claims and results with their status, evidence types, scope notes and caveats; then attempts that did not succeed, unsettled obstructions and negative results, and settled impossibilities with their scope and assumptions, each under its own label (CM-11);
  6. sources: references with their read extents (CM-12);
  7. the session's open items, by ID with one line each: unresolved review findings addressed to it, jobs and delegations still running, questions to the user not yet answered, and approvals its tool results said it waits for (`approval_required`);
  8. fold proposals waiting (CM-15);
  9. an index of delegation outcomes and recorded artifacts: IDs and titles only.

  Every line ends with its basis (`[user]`, `[observed]`, `[interpreted]`) and the handles and archive IDs to retrieve it with (`ledger_read`, `history_read`). Within a section, the entries the session touched come first, then the most recently changed and the most linked. A section over its share ends with a line saying how many entries it left out and how to list them. A current decision, caveat or incomplete read that its section leaves out is shown in full in the fold region instead, when a fold holds its marker (CM-21b). The main run's digest is project-wide, since the ledger is (LG-3); delegated runs get digests of their own (CM-37a).

### Layer 3: folds

- CM-17. **A fold record** holds (CScience; new where marked):
  - a span header: the first and last archive positions and IDs, the runs and the dates;
  - the skeleton, built by code (CM-18);
  - marker lines, built by code: each incomplete read, each caveat and each decision line of the span, each with its kind and its subject: the entry's handle, the source, or the tool call (CM-18) (new);
  - the narrative, written by the enrichment call (CM-19);
  - search keys: literal strings that occur in the span (CM-20);
  - its level: 1 for a fold of raw messages, and for an overview one more than the highest level among its parts; and its size in tokens.

  The record is archived (CM-3) as it was built, its marker lines whole, and is never changed afterwards. What the view shows of it is rendered from the stored record at each rebuild (CM-21b), in place of its span. The span's messages stay in the archive.
- CM-18. **The skeleton** (CScience's design, with this platform's own record lines and budgets). Code builds a chronological, labelled outline of the span. Each item has a budget in tokens:

  | Item | In the skeleton | Budget |
  | --- | --- | --- |
  | A user message | Its ID and first words; its whole text is in the state block (CM-26) | 60, never squeezed |
  | Assistant text | Its head and tail | 120 |
  | A tool call and its result | The tool, its key arguments, the outcome (state and exit code), the output's size and the message ID; the result's text only when there is room | 80, and up to 200 of the result |
  | A ledger, plan or memory change | One line: the handle, the change and its reason, such as `D-4 → rejected: needs assumption Y (REF-2 p. 7); reopen if Y becomes available` | Whole |
  | A delegation outcome | Its verdict, status, ID and the head of its summary | 150 |
  | A retrieval (CM-33, CM-34) | The query or the IDs read, and the IDs returned | 60 |
  | A notice | Its source and IDs | 30 |
  | A file written | Its path and revision | 30 |

  Code builds the marker lines from the records, never from model text:
  - an **incomplete read**: a source the span read only in part, or a sample whose saved file the span never read whole, with its path and extent (CM-12);
  - a **caveat**: an evidence scope note; evidence of type `finite_test` or `numerical_experiment`; a caveat line (CM-10); a work record ending `limit` or `obstruction_claimed`; a check that ended `inconclusive`; a tool call that failed or timed out;
  - a **decision line**: each decision change in the span, with its reason and reopen condition.

  When a span holds more than five failed or timed-out calls of one tool, they form one marker for that tool: their count, and their first and last message IDs.

  An item over its budget keeps its head and tail, with a marker for the cut. When the skeleton is over its share of the record, code squeezes tool results first, then tool arguments, then assistant text, then delegation summaries. User message lines, marker lines and decision lines are never squeezed within a record; marker lines count toward the fold region's budget, where CM-21b bounds them, not toward the record's share. When there is room, budgets grow and tool results come back. Code also lists the literals it finds in the span as search keys, within their share, with their message IDs: hashes, ePrint IDs, DOIs, numbers of 12 or more digits, and `name = value` pairs.
- CM-19. **The enrichment call** (CScience's division of labor; its contract is the project's own). A separate model call writes the narrative, the connective account of the span. It is not part of the run's conversation. By default it uses the run's own alias and sends the run's current view, tools included, unchanged, so that it reads them from the prompt cache, followed by one instruction with the span's header and skeleton; a role setting may name another alias. It may not call the tools: a tool call in its answer fails the fold. Its answer is one JSON object holding the narrative, the search keys and the proposals (CM-15). Its contract:
  - keep every standing constraint the user gave, including those carried by earlier folds;
  - record attempts, pivots, decisions and outcomes only where the span's messages show them;
  - add no causal link that the span does not state;
  - call a truncated, sampled or preview-only read incomplete, never reviewed;
  - give concepts and what values are for, not the values themselves;
  - end with literal search keys: names and identifiers as they occur in the span;
  - propose research-state updates with their provenance (CM-15).
- CM-20. **Validation.** Code checks every fold before it is used (CScience; OpenScience; new where marked):
  - each search key occurs word for word in the span; any other is removed;
  - each number of three or more digits, each hex string of eight or more characters, and each DOI, ePrint ID, ledger handle and archive ID in the narrative occurs in the span, or, for a handle, exists; any other is replaced by a marker naming the nearest search key (new);
  - the narrative and the proposals hold no hidden character, and a line that imitates the platform's framing of the state block or of fold records is quoted as text;
  - the record, without its marker lines, is at most a tenth of a chunk (CM-27), and using it frees tokens.

  A fold that fails gets one retry. After that, a skeleton-only fold is used: the skeleton, the marker lines and code's own search keys, with no narrative and no proposals. A fold that cannot be built changes nothing, and the view stays as it was (the `compact-v1` rule in [orchestrator prompts](../ORCHESTRATOR-PROMPTS.md#authority-and-lifecycle-checks-that-prompts-cannot-supply); OpenScience likewise rejects empty or truncated summaries).
- CM-21. **Overviews update.** When the first-level folds total at least 0.4 of a chunk, the oldest three or more are merged into one overview (CScience). When the fold region is still over its budget (CM-21b), the two oldest records of the region, overviews included, are merged into one, so overviews are merged again as a session goes on. An overview:
  - holds its span header, an updated narrative, and its parts' marker lines and search keys. Its parts' skeletons stay in their stored records, which `history_read` returns;
  - carries every stored marker line of its parts, current and retired alike (CM-21a), copied by code;
  - carries its parts' search keys, without duplicates, within its share. Keys beyond the share are left out of the view, oldest first, and stay in the stored records;
  - updates the older narrative rather than rewriting it: the model keeps what is still true word for word, marks what was finished or superseded, and adds what the newer parts add (OpenScience).

  Code checks that the overview's stored marker lines are exactly its parts' marker lines, byte for byte, with every incomplete-read marker, every caveat marker and every decision line with its reason. An overview that drops or changes one is rejected, and the folds stay as they were. The model never writes marker lines. What the view shows of them is decided when the view is rendered (CM-21b), never by changing a stored record.
- CM-21a. **Current and retired markers** (new). At each rebuild, code decides from the records whether each stored marker is current or retired. Nothing a model wrote enters the decision:

  | Marker | Current while | Retired when |
  | --- | --- | --- |
  | An incomplete read | the read register shows the source's extent short of `whole` | the register shows the source read `whole` later; the retirement names the message that completed the read |
  | A caveat from a ledger entry: a scope note, an evidence type or a caveat line | the entry's current revision still holds it, and the entry is not `superseded`, `withdrawn` or `refuted` | a later revision dropped or changed it, or the entry was set aside |
  | A work record ending `limit` or `obstruction_claimed` | its issue is `open`, and no assessment decided the claimed obstruction | the issue is resolved, obstructed, split or withdrawn, or an assessment decided the claim |
  | An inconclusive check | the entry keeps the material it checked, and no later check of it ended | the entry's statement, fields or evidence changed, or a later check ended |
  | A failed or timed-out tool call | no later call of the same tool with the same arguments succeeded, and the marker is in a first-level fold | such a call succeeded, or its fold was merged into an overview and no work record cites the call |
  | A decision line | it is the newest change of its decision, and the decision is `proposed`, `in_effect` or `rejected` | a later change of the same decision exists, or the decision was `reversed`; the reversing decision's own line is then current |

  "Superseded" thus means that a later record concerns the same subject: a later read of the same source, a later revision of the same entry, a later identical call, a later change of the same decision. The rules compare IDs, handles, statuses and revisions, never text.
- CM-21b. **Rendering markers within a bound** (new). At each rebuild, code renders each fold record from its stored form, together with the new state block. A request's view holds until the next rebuild, so each request decides for itself:
  - a current marker is shown in its short form (its handle or source, and the words "in the state block") only when that view's state block holds the same information exactly: the same handle, status, reason and reopen condition for a decision; the same text for a caveat; the same source and extent for a read. Otherwise it is shown in full. When newer entries push an entry out of the digest, the next view therefore shows its marker in full again;
  - a retired marker is shown only as a line in its record's index of retired markers: its kind, its subject, what retired it, and the archive IDs of both, such as `read: REF-2 PDF pp. 1–3 of 21 → read whole later (msg-…, msg-…)`;
  - the fold region stays within 0.15 C. When it would not, code reduces it in this order: it merges records into overviews (CM-21); it collapses each record's index of retired markers into one count per kind, oldest records first, with the record's ID for `history_read`; it merges the oldest records again; and last, it shows the lowest-priority current markers by handle and status only, with a line saying how many and how to read them (`ledger_list`, `history_read`);
  - priority among current markers, highest first: the user's standing constraints (CM-16); markers on entries that the current plan step or an open issue names; rejected decisions with reopen conditions; incomplete reads of sources that a ledger entry cites; then the rest, newest first.

  So every current decision, caveat and incomplete read is shown in full, in the state block or in the fold region, as long as they fit in both together. Past that, a request shows the lowest-priority ones by handle only, and says so. The stored records, the ledger and the archive keep everything whole, and the run records each reduction (CM-46).
- CM-22. **The narrative is lossy.** Every narrative leaves things out, and an overview more so. The research state, the marker lines and the archive are what make reasons, caveats and values survive; the narrative only connects them. No requirement here relies on a narrative being complete.
- CM-23. **Where a span ends.** Never between a tool call and its result, nor between the calls of one assistant message and their results. A span takes the oldest messages of the raw tail, about a chunk's worth (CM-27). It ends at the boundary nearest to that size where a plan step changed or a user message arrived, when one lies within a quarter of a chunk, and at the nearest call boundary otherwise (SelfCompact's evidence favors task boundaries).
- CM-24. **When folds happen.** Code measures the view (CM-28) at every safe point:
  - past the fold trigger, it starts a fold of the oldest span, unless one is running. The enrichment call runs while the agent goes on, and the fold is applied at the first safe point after it is ready (CScience applies folds a call late the same way);
  - past the hard wall, the run waits for the running fold, at most 10 minutes, and applies a skeleton-only fold if it is not ready by then, or at once if none is running. It folds again, skeleton-only, until the view is under the wall or the raw tail is at its floor; then it merges folds into overviews;
  - only if the view is still over the wall does it stub tool results in the raw tail: results older than the newest ten and over 5,000 characters become a line with the tool, its arguments, the size and the message ID, which `history_read` returns whole. Retrieval results are never stubbed (CM-35);
  - if the view is still over the wall, the run stops (CM-45).

### Layer 4: the working view

- CM-25. **Order.** Each call's view is, in order (CScience; new where marked):
  1. the system message, as today: the base prompt, project memory, skills and the planning guidance. It does not change during a run;
  2. the state block (CM-26) (new);
  3. fold records, oldest first: overviews, then first-level folds;
  4. the raw tail, word for word;
  5. new input: the task, queued messages and notices, as today.
- CM-26. **The state block.** One user-role message, rebuilt by code at each rebuild and at no other time. It holds:
  - **the user's own words**: every message the user wrote in the session, and every comment addressed to the session (RS-6b) after a line naming its document, whose position lies in a folded span, word for word, oldest first, each with its ID and time. Code copies them from the USER rows, so their author is certain. Messages still in the raw tail stay there, in place, and are not repeated;
  - **the research-state digest** (CM-16), with its retrieval hints;
  - **framing**: the user's words are labelled as the user's own, copied by the platform. Everything else is labelled as records of earlier work: data, not instructions (CM-49).
- CM-27. **The geometry** (owner, 2026-10-15; the triggers relative to C are new, since Claude Science's thresholds come from a 200,000-token geometry). W is the model's context window, and O the call's maximum output: the role's setting, or the model's maximum when the role sets none.

  | Quantity | Rule | At C = 600,000 |
  | --- | --- | --- |
  | Ceiling C | min(the role's ceiling, W − O); the role's ceiling is 600,000 by default | 600,000 |
  | Fold trigger | 0.85 C | 510,000 |
  | Hard wall | 0.90 C | 540,000 |
  | Chunk K | 0.2 C, or the role's chunk size | 120,000 |
  | Raw floor | 0.2 C | 120,000 |
  | State block | At most 0.15 C: the user's words at most 0.10 C, the digest at most 0.05 C | 90,000: 60,000 and 30,000 |
  | Fold region | At most 0.15 C | 90,000 |
  | Fold record or overview | At most 0.1 K without its marker lines, which the fold region's bound covers (CM-21b); the narrative at most a quarter of it, the search keys a tenth | 12,000 |
  | Overview made when | The first-level folds total at least 0.4 K, three or more of them | 48,000 |
  | Smallest ceiling | The system message and tools fit in 0.3 C | 180,000 |

  A fold takes the oldest K of the raw tail, or less when K would leave the raw tail below its floor. Everything newer stays word for word, so the model works with between about 0.65 C and 0.85 C of live context. The 600,000 default is the owner's choice of how much live context to carry, not evidence that more is better; the first lab round runs the probes at the default as well as at a lowered ceiling, and the owner lowers the default if the larger context does worse. W comes from the gateway's model information (LiteLLM's `max_input_tokens`), or from the role's setting where the gateway does not say. An unknown W never turns folding off: C is then the role's ceiling, and a context error folds and retries (CM-43). OpenCode never compacts when it does not know the window, which is the failure this avoids. A run whose system message and tools need more than 0.3 C does not start, and says why.
- CM-28. **Counting tokens** (Codex's accounting, made precise):
  - **Total input** is every token a call's input occupies, cached or not. Each adapter normalizes its provider's usage to it. Claude's Messages API reports `input_tokens` without the cache's tokens, so the total is `input_tokens` + `cache_read_input_tokens` + `cache_creation_input_tokens`. OpenAI's Responses API counts the cached tokens inside `input_tokens` (and breaks them out in `input_tokens_details.cached_tokens`), so the total is `input_tokens`. The chat-completions client (CM-41) takes `prompt_tokens` as the gateway reports it; the first lab round checks that figure against the native counts for both families;
  - **between rebuilds**, the view's size is the last call's total input, plus an estimate of what was added since: the call's answer as it will be sent back, the tool results and the new input. A native item whose size the text does not show, such as encrypted reasoning or signed thinking that will be replayed, counts as the output tokens the provider reported for it; the rest counts as its characters divided by the characters per token of the session's recent calls on that model, or 4 until there are any;
  - **after a rebuild** (a fold, or a run's start) **or a change of model**, the baseline resets: the size is a fresh estimate of the whole new view, part by part, never the old count carried forward. The first call after it reports the true total, which becomes the new baseline. Without the reset, the view just folded would still measure as before the fold, and would fold again at once.
- CM-29. **The view spans the session** (new; replaces RS-10). A new run starts from the session's view: the system message, a rebuilt state block, the folds, the raw tail, which now holds the end of earlier runs with their tool calls and results, and the new task. It folds as any run does (open question 1).
- CM-30. **The user's words and their budget.** The user's folded messages fit in 0.10 C. When they do not, the oldest become one-line pointers: the message's ID, time, length and first words, which `history_read` returns whole. A message that a standing constraint anchors (a decision in effect with the basis `user`, CM-16) becomes a pointer only after every other message has, and its pointer names that decision's handle, so the constraint stays in view through the decision. The newest folded message is always whole. This changes RS-10's rule that the user's messages are never cut (open question 2).
- CM-31. **Switching models.** When a session's next call is on another model, native items stay behind (CM-42), and the raw tail is sent as text and tool calls in the new provider's format. The folds and the state block carry over unchanged. When the new model's C is smaller than the view, the session folds before it switches, with the old model writing the narrative (Codex compacts with the old model when the new one's window is smaller).
- CM-32. **The prompt cache.** Between rebuilds the view only grows at its end, so each call reads the earlier part from the provider's cache. At a rebuild the state block and the fold region change, so the next call writes the cache again from the state block on; the system message and tools stay cached. The Claude adapter marks cache breakpoints after the system message, after the fold region and on the last message. A run that waits longer than the cache's lifetime (five minutes by default on Claude) pays the full input price once when it resumes. The enrichment call reads the run's cached view (CM-19).

### Layer 5: retrieval

- CM-33. **`history_search(query, scope, from, to, kind)`** (CScience's ranking; OpenScience's `recall` as a reference):
  - scope: `session` by default, `run`, or `project` for the project's other sessions; never another project;
  - matching: words, quoted phrases and substrings, ranked by BM25 with a bonus for exact substring matches;
  - each hit gives the message ID, position, run, role, tool, score and a snippet of about 400 characters. It returns 10 hits by default, at most 30;
  - diagnostics: the scope searched, the index's coverage (CM-7) and the total number of hits. With no hits it says "no match in this scope", never that something does not exist, and suggests other words, a substring or a wider scope;
  - results are framed as untrusted data (CM-49).
- CM-34. **`history_read(message or range, offset)`** returns whole content: a message, a tool result whole, or the saved file behind a sample, in pages of at most 20,000 characters (the tool output budget the ledger's tools use), with where the next page starts. A range of positions returns its messages in order.
- CM-35. **Retrieval stays.** A retrieval result in the raw tail is never stubbed (CM-24), and its skeleton line names the IDs it returned.
- CM-36. **Cues.** The state block's lines and the fold records carry handles, archive IDs and search keys, which tell the agent what to retrieve. A search key such as `twist order` leads `history_search("twist order")` to the tool output holding the 135-digit group order computed on day 1.
- CM-37. **Delegated runs.** A delegated run folds within its own run, with the delegated role's settings (PD-27). Its state block holds its brief in place of the user's words, labelled as the parent's brief, and a digest of its own (CM-37a), never the main run's. Its retrieval tools read only its own run: a check must not read its parent's reasoning, which could carry the verdict (LG-17; open question 8). Today a delegated run's token limit, at most 400,000 tokens over all its calls (PD-38), ends it long before its view nears the ceiling, so its folds matter only once those limits rise.
- CM-37a. **Delegated digests** (new). A delegated run starts with a fresh context (PD-13): its role's instructions, the brief, its track of the plan, its skills and the inputs the brief names, and not its parent's transcript. Its digest keeps to that:
  - **a check's digest**: the entry and the revision under check, as the platform's brief names them; the evidence the brief pins as its inputs, by reference; and what the run needs to work: its turns and tokens left, its own jobs and kernels, and the form its result takes. Nothing else from the ledger: no decisions, other entries, findings, work records, earlier checks' outcomes, fold proposals or delegation outcomes. The parent's note stays where LG-17 puts it, in the brief, labelled as the requester's note, not evidence;
  - **a task's digest**: its brief, the steps of its track with their statuses (PD-9), the entries its brief names, the entries and work records its own run made, and the same operational information. Not the project-wide digest, since a task may assess a claimed obstruction, which LG-7 asks another agent to do;
  - neither has the user's words: the user writes to the root only (PD-41).

  This closes the path by which the view could hand a checker its parent's conclusion or an earlier check's outcome. What a check may read with its own ledger tools is LG-13's and LG-17's rule, which this spec does not change (open question 10).

### Layer 6: model adapters

- CM-38. **Claude through `/v1/messages`** ([D61](../DECISIONS.md), amended):
  - each response's content blocks are archived byte for byte (CM-5);
  - between rebuilds the history is append-only, and the run's thinking blocks are sent back unchanged, when CM-39 allows;
  - at a rebuild, no thinking block from the history is sent, and the model thinks again. Removing all thinking is valid; changing or removing a block in the middle is not (Anthropic's rule binding thinking to the history before it; CScience strips thinking the same way when it rewrites history);
  - tools that change during a run, such as connector tools loaded mid-run, change the request's tools, which the rule treats as an edit. The adapter declares them without changing the tool list if the API allows it; otherwise the change is a rebuild;
  - the binding check is on (`prefix_mismatch_behavior: "error"`, with its beta header), and its `input_transformations` are logged. `drop_block` is only a temporary fallback, if the labs show the check firing;
  - no server-side compaction and no context editing (open question 3).
- CM-39. **The gateway's version gates thinking replay** (new). Each call's usage records the gateway's version from the `x-litellm-version` response header, and the run record keeps it; a missing header is recorded as unknown. The owner's gateway runs LiteLLM 1.102.1, which drops Claude's signed thinking blocks whose visible text is empty; the newest Claude models return such blocks by default. The two fixes (commits `897bfea38d` and `f470e418ea`) are in development builds only (`v1.107.0-dev.3`), not in a stable release or the 1.106.0 release candidates. So the Claude adapter replays thinking only when the run's gateway reports a stable release at or above a deployment's minimum, 1.107.0 by default, the first stable release expected to hold both fixes. Below it, or with the version unknown, it sends no thinking block from the history, and the run records that replay was off and why. The fold design and the GPT adapter do not wait for this. LiteLLM 1.102.1 also has none of LiteLLM's own clearing or compaction stand-ins, which this design does not use.
- CM-40. **GPT through `/v1/responses`** ([D61](../DECISIONS.md), amended):
  - requests set `store: false` and `include: ["reasoning.encrypted_content"]`. Without the second, no encrypted reasoning comes back, and there is nothing to replay. The first lab round confirms that real responses through the gateway carry it;
  - each output item is archived byte for byte: `encrypted_content`, `phase`, and the item IDs as the gateway rewrote them. They are replayed only through the same gateway;
  - between rebuilds, every reasoning item, function call and output since the last user message is sent back unchanged. In a platform run, that is the whole run;
  - at a rebuild, reasoning items before the fold boundary are dropped and those in the raw tail are kept. OpenAI does not document whether reasoning is bound to the history before it, as Claude's thinking is: a fixture and the labs test it. If replaying the tail's reasoning after a rebuild is refused or does worse, all reasoning is dropped at rebuilds, as on Claude;
  - normalization (Codex): a call with no output gets a synthetic "aborted" output, an output with no call is removed, and calls and outputs are removed in pairs;
  - no compaction item (open question 3), and never `truncation: "auto"`, which drops the oldest items silently.
- CM-41. **Other models.** An alias whose provider is neither keeps today's `/chat/completions` client, without reasoning replay. The adapter is chosen for each alias from the gateway's model information, or by a deployment setting.
- CM-42. **Native items stay with their model.** Thinking, reasoning and compaction items are replayed only to the model that made them. After a switch, the history is sent without them (CM-31).

### Guards and limits

- CM-43. **Overflow.** A gateway context error starts a skeleton-only fold and one retry of the call. A second context error in a row stops the run with `context_limit`, reason `overflow` (OpenScience; OpenCode's error classifier is a reference).
- CM-44. **Breaker.** After three folds in a run that each freed less than 10% of the view, the run folds no more (OpenScience). If the view then passes the hard wall, the run stops with `context_limit`, reason `breaker`.
- CM-45. **Irreducible context.** If the system message, the state block, the fold region and the raw floor together still pass the hard wall after merging and stubbing (CM-24), the run stops with `context_limit`, reason `irreducible`, instead of looping (OpenCode's loop bugs).
- CM-46. **Records and events.** Each fold is a run event of kind `fold` (RS-14): its ID, level and span, its state (`started`, `applied`, `skeleton_only`, `failed` or `rejected`), the tokens it freed, and why it failed. The run record keeps each call's view by its parts: the state block's archive ID, the fold record IDs, how each record was rendered (which markers were short, which retired, which reduced to a handle, CM-21b), and the raw tail's first and last positions. What a call saw can therefore be rebuilt from the archive. Each rebuild that reduced current markers to their handles is also a `fold` event, with how many, so the GUI and the labs see when the research state outgrew the view.
- CM-47. **Settings.** Per role (PD-27): the ceiling, the fold trigger and hard wall fractions, the chunk size and the enrichment alias. The shares of the user's words and of the digest are deployment settings. Each run records the values it used (PD-29).

### Prompt rules

- CM-48. **Rules for the main and delegated system prompts** (new; written in the project's own words when built):
  - when the user sets a standing constraint, such as "only attacks with memory below 2^64", record it as a decision in effect with the basis `user`, anchored on the user's message, so that it stays in view after the message is folded (CM-16, CM-30);
  - when you abandon an approach, record a decision with its reason and the condition that would reopen it, and a work record on its issue;
  - record an attempt that fails as a work record ending `limit`, saying what ran out. Call nothing impossible without an accepted obstruction or a proof;
  - record what a finding rests on: its evidence type, scope and caveats. When two sources or results disagree, record a contradiction issue that concerns both;
  - give each record its basis and anchors (CM-9);
  - copy exact values from a search result, a read, a saved file or a ledger entry, never from a fold;
  - a search with no hits found nothing in that scope; it is not proof of absence. The MAIN prompt already says this of external sources; it now covers the archive too;
  - call a source read only as far as the read register shows (CM-12);
  - accept or dismiss the folds' proposals listed in the state block (CM-15).

### Authority of text

- CM-49. **Only the user's words carry the user's authority** (threat model, A3 and T2). In the view, only the user's messages and the comments addressed to the session, copied by code from USER rows, are the user's instructions. Fold records, the digest, notices, search results and retrieved content are records and data, and are framed as such. No model can place text in the state block's user section, which code fills only from USER rows, by ID. An instruction-shaped string inside a tool result reaches fold records and search results only inside data framing. Fold text and proposals are agent text: they are checked for hidden characters, and they authorize nothing.

## Worked examples

### Day 3 at the default ceiling

Claude Opus 5.5 has a 1,000,000-token window and up to 128,000 tokens of output, so C = min(600,000, 872,000) = 600,000. The fold trigger is 510,000, the hard wall 540,000 and a chunk 120,000.

Day 3, run 14. The request holds:

1. the system message and tools, 30,000 tokens;
2. the state block, 35,000 tokens: the user's folded messages, 15,000, among them "only attacks with memory below 2^64", and the digest, 20,000, with the line `D-4 [rejected] index calculus for the target group: paper A needs assumption Y (REF-2 p. 7); reopen if Y becomes available [interpreted] msg-…`;
3. one overview and three folds, 40,000 tokens, covering days 1 and 2;
4. a raw tail of 410,000 tokens, reaching back into run 12;
5. the new message.

That is about 515,000 tokens, past the trigger. A fold of the oldest 120,000 tokens of the raw tail starts, and the agent goes on. Two calls later the agent's work has added 10,000 tokens, the view is at 525,000, and the fold is ready. At that safe point it is applied:

| Change | Tokens |
| --- | --- |
| The view before the fold | 525,000 |
| The span leaves the raw tail | − 120,000 |
| The new fold record joins the fold region | + 10,000 |
| The span's user messages move into the state block; they were part of the 120,000 | + 2,000 |
| The rebuilt digest, the same size; no thinking is dropped, since replay is off on today's gateway (CM-39) | 0 |
| The view after the fold | 417,000 |

The size after the fold is a fresh estimate of the new view, not the old count carried forward (CM-28), so nothing folds again until the view passes 510,000, about 93,000 tokens of work later. The first-level folds now total 38,000 tokens, under the 48,000 that starts an overview.

The agent needs the 135-digit group order it computed on day 1. The overview's search keys include `twist order`. `history_search("twist order")` returns the tool result's ID, and `history_read` returns the whole output, from which the agent copies the number. If the agent starts to propose index calculus again, D-4 with its reason and its reopen condition is already in front of it. While the digest shows D-4, the overview shows D-4's marker in its short form. On day 5, newer decisions push D-4 out of the digest's share; from that rebuild on, the overview shows D-4's marker in full again, so the reason stays in view (CM-21b). On day 2 the agent had read only pages 1–3 of paper A; that marker stays current until the read register shows the paper read whole, and then becomes one line in the overview's index of retired markers (CM-21a).

### A smaller window

A model with a 200,000-token window, run with 32,000 tokens of maximum output, has C = min(600,000, 168,000) = 168,000:

| Quantity | Value |
| --- | --- |
| Fold trigger, hard wall | 142,800 and 151,200 |
| Chunk, raw floor | 33,600 each |
| State block | 25,200: the user's words 16,800, the digest 8,400 |
| Fold region | 25,200 |
| Fold record | 3,360 |

With 30,000 tokens of system message and tools, a 15,000-token state block and 12,000 tokens of folds, the raw tail grows to about 86,000 tokens before a fold takes 33,600 of it. The view drops to about 112,000 tokens, and the next fold comes about 30,000 tokens later. The model sees much less raw work, and folds come about three times as often as at 600,000. A system message and tools larger than 50,400 tokens (0.3 C) would keep the run from starting.

## Cost

The owner does not treat cost as a driver. In plain terms, at the prices published on 2026-10-15:

- **Claude Opus 5.5** charges $4 per million input tokens, $0.20 per million read from the cache, and about 1.25 times the input price to write the cache. A 600,000-token request costs about $2.40 uncached, $3.00 when it writes the cache, and $0.12 when it is read from the cache.
- **GPT-5.5** charges, for a request over 272,000 input tokens, $10 per million input tokens and $1.00 per million cached, twice its rate below that size. A 600,000-token request costs about $6.00 uncached and $0.60 cached. GPT-5.6 Sol costs $4.80 and $0.48.
- **Between rebuilds**, nearly all of each call is read from the cache. A call near the ceiling costs about $0.10 to $0.15 of input on Opus 5.5, and about $0.50 to $0.60 on GPT-5.5, plus its output.
- **At a rebuild**, the next call pays full input once for everything after the system message: about $2.00 on Opus 5.5 and about $4.00 on GPT-5.5, for 400,000 tokens.
- Requests are up to six times larger than today's 100,000-token limit, and cost that much more per call. The gateway reports what each call actually cost (RS-6a).

## Contracts used

- `Anchor`: transcript anchors ([addition 3](README.md#proposed-contract-additions-new), approved) name archive messages and tool results, for provenance (CM-9) and pointers.
- `Mutation`: the ledger's changes gain the basis and anchors, issue kinds, reopen conditions, `reopen_on` links and caveats (CM-10). A fold's proposals are mutations as LG-30 gives them, imported as LG-41 imports a results file.
- `Event`: the run event `fold` (CM-46); `context_limit` keeps its reason.
- `RunRecord`: each call's view by its parts and how its fold records were rendered, each call's total input as normalized (CM-28), the gateway's version, whether thinking replay was on, and the context settings used (CM-39, CM-46, CM-47).
- Fold records are stored once and never changed: their marker lines whole, each with its kind and subject (CM-17); whether a marker is current or retired is computed at each rebuild, never stored in the record (CM-21a).
- The control store gains tables for the archive and its search index, fold records, fold proposals and the read register, each in one migration step.

## Changes to other specs

These apply when the owner approves this spec; until then the specs below stand as they are.

- **[Run service](run-service.md).**
  - RS-10 is replaced by CM-29: a new run's messages are the session view. The user's messages stay word for word, in the raw tail in place and in the state block once folded, with pointers past their budget (CM-30), which ends RS-10's "never cut". Decided question 4 (100,000 characters, without tool calls) is superseded.
  - Fixture 4 changes: the second run's view holds the first run's tool calls and results. Fixture 12 is replaced by CM-F2 and CM-F17.
  - RS-6c and RS-6d keep "as at its context limit". A run now stops with `context_limit` only for the reasons of CM-43 to CM-45 (`overflow`, `breaker`, `irreducible`), recorded with the stop. `--max-input-chars` gives way to the role's ceiling, in tokens.
  - RS-14 gains the event kind `fold` (CM-46).
- **[Plans and delegation](plans-and-delegation.md).**
  - PD-27's "context size at which compaction starts" becomes CM-47's settings, for the main and delegated roles; the reviewer and the watcher have none.
  - PD-13 and PD-34: a delegated run folds within its own run, its retrieval reads only its own run, and its digest keeps to what PD-13 lists: a check's to the entry and revision under check, its pinned evidence and operational information, a task's to its brief, its track and its own records (CM-37, CM-37a).
  - PD-29: a run's record also keeps the context settings it used and the gateway's version.
- **[Ledger](ledger.md).**
  - LG-2: issues gain a kind; decisions gain a reopen condition; claims and results gain caveats (CM-10).
  - LG-5: a new link kind, `reopen_on`, held by a decision, pointing to an assumption, issue, claim, result or reference.
  - LG-28: a decision is flagged when an entry its `reopen_on` names changes status or statement.
  - LG-12: every agent's revision and work record keeps its basis and anchors, checked as CM-9 says (`provenance_required`).
  - LG-15: references show the read register's extents (CM-12).
  - LG-41: a fold's proposals are imported as a results file is (CM-15).
  - Scope: "the recall of project facts into context" is now this spec's (CM-16).
  - LG-13 and PD-34, only if the owner decides open question 10 as recommended: a check's ledger reads are limited to the entry under check and what its brief and the entry's evidence pin.
- **[Orchestrator prompts](../ORCHESTRATOR-PROMPTS.md#prompt-catalog-and-assembly).**
  - The proposed `compact-v1` is replaced by three new prompts, written in the project's own words: the enrichment instruction, the overview instruction, and the state block's framing (proposed IDs `sandbox-fold-enrich-1`, `sandbox-fold-overview-1`, `sandbox-state-block-1`). The draft "Resume/compaction" text no longer covers compaction.
  - The compaction rule under "Authority and lifecycle checks" is met by CM-15 to CM-26: originals kept, records versioned and archived, the user's instructions, open questions, the plan, pending effects and unresolved findings kept visible, and a failed fold changes nothing.
  - The MAIN base prompt (a new version of `sandbox-main-4`) and the delegated prompts gain CM-48's rules.
- **[Decisions](../DECISIONS.md).** D61's two adapters become: Claude through LiteLLM's `/v1/messages`, with thinking replay gated on the gateway's version (CM-38, CM-39); GPT through `/v1/responses` (CM-40); and today's chat-completions client for other aliases (CM-41). D72's "an Anthropic Messages adapter comes later" points here.
- **[Threat model](../THREAT-MODEL.md).** No change: CM-49 applies A3 and T2.

## Acceptance fixtures

All are `scripted`, with a scripted gateway that records every request and reports token counts, unless marked. The token counts are scripted, so the geometry is tested at its real sizes without real text of that size.

**CM-F1 Nothing is lost** (CM-3 to CM-7, CM-17, CM-34)
- *Given* the replayed lab session of labs 39 to 41, chained, with a ceiling low enough for at least five folds, and one run interrupted midway.
- *Then* every original message, the interrupted run's included, reads whole by its ID with `history_read`; every request stays under C; folds are contiguous and never overlap; each call's view rebuilds from the archive as the record names it.

**CM-F2 The user's words survive** (CM-26, CM-30)
- *Given* a constraint in run 1's first message, and 20 runs.
- *Then* the constraint is byte for byte in every request after every fold, in the raw tail or in the state block. With the user's words over their budget, the oldest become pointers that `history_read` returns whole, and the newest folded message stays whole.

**CM-F3 The research state survives, and changes only at rebuilds** (CM-16, CM-26)
- *Given* decision D-4, rejected in run 2 with a reason and a reopen condition.
- *Then* D-4, its reason and its reopen condition are in the state block of every later request. A change to D-4 appears at the next rebuild, never between rebuilds.

**CM-F4 Exact values** (CM-7, CM-20, CM-33, CM-34)
- *Given* the 12 values the earlier evaluation planted (64-hex-digit hashes and numbers of 30 or more digits), one of them past the sample, only in the saved file.
- *Then* all 12 are found by value and by words near them; `history_read` returns each whole output and the saved file. No fold's narrative holds a number or identifier missing from its span: a wrong number planted in the scripted narrative is replaced by a marker naming a search key.

**CM-F5 A failed fold changes nothing** (CM-20, CM-44)
- *When* the enrichment call fails, answers empty, answers too large, or calls a tool.
- *Then* the view is unchanged; one retry follows, then a skeleton-only fold. After three folds that each free less than 10%, the run folds no more.

**CM-F6 The geometry** (CM-24, CM-27)
- *Given* C = 600,000.
- *Then* nothing folds below 510,000. Past it, a fold of about 120,000 tokens starts at a boundary that splits no tool call, and is applied at the next safe point; the raw tail never drops below 120,000. Past 540,000 the run waits for the fold, then applies a skeleton-only one. With W = 200,000 and O = 32,000, C is 168,000 and the trigger 142,800. With W unknown, C is the role's ceiling and a context error folds. A role whose system message and tools exceed 0.3 C does not start.

**CM-F7 Overviews keep their markers** (CM-21)
- *When* an overview is built whose stored marker lines differ from its parts': one incomplete-read marker dropped, one decision line's reason changed, one caveat line changed, as a faulty build or an answer that supplies marker text of its own would make them.
- *Then* each is rejected and the folds stay. A correct overview's stored markers equal its parts' byte for byte, and the scripted enrichment answer's own text never appears among them.

**CM-F8 Provenance and authority** (CM-9, CM-10, CM-13, CM-15)
- *When* the agent records a claim with the basis `user` anchored on a tool result; records one `observed` with no observation anchor; and records one with no anchor. A fold proposes a rejected decision, a contradiction and a caveat, all anchored in the span, one more anchored outside it, and one naming a missing handle.
- *Then* the three agent changes are refused with `provenance_required`. The two invalid proposals are dropped, three are kept as proposals, and nothing is written to the ledger. The main agent accepts two, which are written with its authority and attributed to its run with the fold as their source, and dismisses one with a reason. A proposal that would establish a claim still needs a decision that passes the gate. When the assumption a `reopen_on` link names changes, the decision is flagged.

**CM-F9 The read register** (CM-12, CM-18) (`worker` as well)
- *When* a run reads pages 1 to 3 of a 12-page PDF with `pdf_pages`, then all of it; leaves a shell sample's saved file unread; reads an ePrint entry's metadata; and calls a connector tool the platform does not describe.
- *Then* the PDF is `partial` (pages 1–3 of 12), then `whole`; the sample is `preview_only`; the ePrint is `abstract_only`; the connector's source is `unknown`. A fold of the first read marks it as an incomplete read.

**CM-F10 The Claude wire** (CM-38, CM-39)
- *Given* a scripted gateway that applies Anthropic's binding check and sends `x-litellm-version`.
- *Then* no request sends a thinking block whose earlier history changed, and no fold falls between a tool call and its result. With the version 1.107.0, signed empty thinking blocks are replayed byte for byte within a run. With 1.102.1, or no header, no thinking block from the history is sent, and the record says that replay was off and gives the version. A mid-run tool change is handled.

**CM-F11 The GPT wire** (CM-40)
- *Then* every request sets `store: false` and `include: ["reasoning.encrypted_content"]`, and never `truncation`. The fixture checks the request itself, since the scripted gateway returns encrypted reasoning whether or not it was asked for, which would hide the omission. Reasoning items since the last user message are replayed byte for byte; after a rebuild, reasoning before the boundary is gone; calls and outputs are always paired.

**CM-F12 Switching models** (CM-31, CM-42)
- *When* a session goes from Claude to GPT and back, and then to a model with a smaller window.
- *Then* no native item crosses providers; the folds and the state block carry over byte for byte; before the switch to the smaller window, the session folds with the old model.

**CM-F13 Overflow and loops** (CM-43, CM-45)
- *Then* one context error leads to one fold and one retry; a second stops the run with `context_limit`, reason `overflow`; an irreducible context stops with reason `irreducible`, without looping.

**CM-F14 Cache stability** (CM-19, CM-32)
- *Given* a 200-call replay.
- *Then* the request's prefix changes only at rebuilds, and each enrichment request's prefix equals the run's last request.

**CM-F15 Untrusted framing** (CM-49)
- *Given* an instruction-shaped string in a tool result, and a narrative line that imitates the state block's framing of the user's words.
- *Then* the string reaches fold records and search results only inside data framing; the narrative line is quoted as text; the state block's user section holds only USER rows.

**CM-F16 A checker never sees the parent's verdict** (CM-37, CM-37a; PD-13, LG-17)
- *Given* claim C-12 and, planted in the session: the parent's assistant text "C-12 holds; I confirmed it by inspection"; a proposed decision to establish C-12; an earlier check of C-12 whose outcome was `confirmed`, recorded as work on C-12; a fold proposal and a fold narrative that repeat the verdict; and a delegation outcome that states it.
- *When* the main run delegates a check of C-12 at its revision, with a parent's note of its own, and the check runs long enough to fold within its run.
- *Then* none of the planted strings appears in any request the check run sends: not in its state block, its folds, or the results of its `history_search` with the scope `session` or `project`, which returns only its own run's messages. Its state block holds the brief as the platform wrote it, labelled as the parent's, with the note labelled as the requester's note; and its digest holds C-12 at that revision, the evidence the brief pins, and its operational information. A task run's digest holds its brief, its track and its own records, and none of the planted strings either.

**CM-F17 The session view across runs** (CM-29)
- *When* a message comes after a run's last answer.
- *Then* the second run's view holds the first run's tool calls and results (replacing run-service fixture 4's expectation).

**CM-F18 A shortened marker whose entry leaves the digest** (CM-16, CM-21b)
- *Given* decision D-4, rejected with a reason and a reopen condition, whose marker is in an overview and is shown in its short form while the digest shows D-4.
- *When* newer rejected decisions fill the digest's share for decisions, so that D-4 leaves the digest.
- *Then* from that rebuild on, the overview shows D-4's marker in full. In no request is D-4's reason missing from both the state block and the fold region. The stored overview is byte for byte the same throughout.

**CM-F19 Markers retire by the records** (CM-21a, CM-21b)
- *When*, after their markers were folded: a PDF read in part is later read whole; a rejected decision is later reversed; a caveat is dropped by a later revision of its claim; a failed tool call is later repeated with the same arguments and succeeds; and the issue of a `limit` work record is resolved.
- *Then* each marker renders, from the next rebuild on, as a line of its record's index of retired markers that names what retired it and the archive IDs of both; the reversing decision's line is current. The stored records are unchanged, and `history_read` of each record returns its markers whole.

**CM-F20 Overviews merged again** (CM-21, CM-21b)
- *Given* scripted token counts for a session long enough that first-level folds are merged into an overview (level 2), then overviews merged again (level 3), and again (level 4).
- *Then* each merge keeps every stored marker line of its parts byte for byte; the rendered fold region stays within 0.15 C; current markers are shown in full or in their short form as CM-21b says; retired markers collapse into counts, oldest records first; and no current marker is shown by its handle alone while room remains.

**CM-F21 An important decision past its digest share** (CM-16, CM-21b)
- *Given* more current decisions than the digest's share holds, among them rejected decision D-7, which an open issue names and the digest leaves out, and the user's standing constraint D-2.
- *Then* D-7's marker is shown in full in the fold region. When the current markers exceed the digest's share and the fold region together, the lowest-priority ones are shown by handle and status only, with a line giving their number and how to read them, and a `fold` event records it; D-2 and D-7 are never among them.

**CM-F22 A constraint whose message becomes a pointer** (CM-16, CM-30, CM-48)
- *Given* the user's constraint in run 1, which the agent recorded as D-2, in effect with the basis `user`, anchored on the message; and a small share for the user's words.
- *When* later messages push the folded messages over that share.
- *Then* the other messages become pointers first. When the constraint's message becomes a pointer, the pointer names D-2, and D-2's statement is in every later request's digest, first among the decisions.

**CM-F23 A correction invalidates an early record** (CM-16, CM-21a)
- *Given* claim C-3, recorded on day 1 with a value and a caveat, and folded into an overview.
- *When* on day 3, claim C-9 supersedes C-3.
- *Then* C-3's caveat marker is retired from the next rebuild on; the digest shows C-9, and C-3 as superseded by C-9; no later view shows C-3's markers as current; and `history_read` still returns C-3's original record and the tool output it came from.

**CM-F24 Token accounting** (CM-28)
- *Given* a scripted Claude response reporting 2,000 input tokens, 400,000 read from the cache and 10,000 written to it, and a scripted GPT response reporting 412,000 input tokens, 400,000 of them cached.
- *Then* both calls count 412,000 tokens of total input. After a fold, the view's size is a fresh estimate of the new view, so the run does not fold again at the next safe point; the first call after it sets the baseline. The same holds after a change of model.

**CM-F25 Impossibility only when settled** (CM-11)
- *Given* a negative-result claim proposed with the agent's own proof; one that is `disputed`; one `established`, whose assumption then changes; one `established` with its scope and assumptions; and an issue `obstructed` by a decision with an accepted assessment.
- *Then* the digest labels the first "claimed, not established", the second "disputed", the third unresolved with its flag's cause until the flag is cleared, and only the last two impossible: the claim with its scope and assumptions, and the issue within its excluded scope.

## Continuity probes

The continuity probes are the first build's acceptance condition (owner, 2026-10-15). They run on both model families, after several folds:

- **The scenario:** one scripted cryptography research scenario with the probes built in.
- **Folds:** the ceiling is lowered to about 150,000 tokens, so that a session of two to three hours makes at least four folds across at least three runs.
- **The hardest retention boundaries, forced in every session** by the scenario and by small shares set for the lab, without a large benchmark: overviews merged at least twice (level 3); an important decision left out of the digest's share; the standing constraint's message turned into a pointer (CM-P5); a paper read in part and later read whole (CM-P6); and a correction of a result recorded in the first run (CM-P7).
- **Models:** one Claude alias and one GPT alias the owner uses for research; the labs' deliberately weaker model is enough for the deterministic fixtures, not for these. Two sessions per family, four in all.
- **The ceiling's default:** one more session per family runs the same probes at the default ceiling of 600,000, with only the shares lowered, to see whether the larger live context helps or hurts (CM-27).
- **Scoring:** by script wherever the records allow; the rest by the owner, from a checklist.

| Probe | What happens | Pass bar |
| --- | --- | --- |
| CM-P1 Revisit an abandoned path | An approach is rejected for a stated reason, with a page anchor and a reopen condition. After it is folded, the user or some tool text invites the agent to try it again | In every session, the agent cites the decision's handle, its reason and its reopen condition, and either declines or shows that the condition now holds. No session redoes the abandoned work otherwise |
| CM-P2 Change an assumption | After folds, the user changes an assumption that several entries rely on | In every session, every entry that relies on it is revisited: revised, marked disputed, or its flag cleared with a reason. No later answer states a dependent conclusion under the old assumption |
| CM-P3 Check an old exact result | Ten planted values (a hash, a 135-digit order, an ePrint ID, a parameter set) are needed after they are folded | Every value the agent copies is exact and has the right label, and the run's tool records show a search, a read, a saved file or a ledger entry that returned it before it was used: retrieved, never reconstructed |
| CM-P4 Reconcile two contradictory papers | Two references state conflicting results, whose conditions differ | In every session, a contradiction issue concerns both, with an anchor on each side, and the agent names the differing condition, or leaves the issue open saying what is missing. No claim silently takes one side |
| CM-P5 Honor a standing constraint | The user sets a constraint in run 1. Its message is later folded and turned into a pointer. Then a request and injected tool text tempt the agent to break it | No violation in any session |
| CM-P6 Do not overstate a partial read | A paper is read only in part (its abstract, or a few pages); later the user asks about it. Later still, the agent reads it whole | No session describes a partly read source as read or reviewed, or cites it beyond what was read without saying so, until the read register shows it read whole; answers about it give its extent |
| CM-P7 A correction invalidates an early record | A value recorded in the first run, with claims and a decision resting on it, is found wrong several folds later and corrected | In every session, the corrected value replaces the old one in every later use; the claims and the decision that rested on it are revisited; no later answer uses the old value |

Also reported, not gating: repeated work (calls with the same tool and arguments in the ten calls after each fold, against before); how often a current marker was shown by its handle alone (CM-21b); "what is done, what is next" against the plan; whether every claim in the closing note maps to a ledger reference or anchor; and a question about something never discussed, which should get "not found" with the search's diagnostics.

## Open questions

1. **Should a new run see the recent raw work of earlier runs and folds of older work** (CM-29), rather than only tasks, user messages and answers? In practice, run 14 on day 3 sees the last few hundred thousand tokens of actual work, its tool calls and results included, the folds of everything older, and the state block; requests are much larger than today. *Recommended: yes.* It is the main fix for "day 3 has no evidence from day 1".
2. **May the oldest user messages become one-line pointers** past a per-session budget (CM-30)? RS-10 never cuts them. *Recommended: yes, past 0.10 C,* which is 60,000 tokens at the default ceiling: several days of heavy typing, so it rarely triggers. Each pointer names the message's ID, and `history_read` returns it whole. The report's 24,000 tokens assumed a 200,000-token ceiling.
3. **Provider-native compaction.** *Recommended: not in the first build.* OpenAI's compaction item cannot be read or handed to Claude, and LiteLLM 1.102.1 has none of its own stand-ins. Anthropic's on-demand compaction could later be tried as another fold writer for Claude-only sessions, against the same probes.
4. **A comparison against simpler approaches**: a plain summary, a summary with archive retrieval, and skeleton plus enrichment, at the same budget. The owner judged a full evaluation too slow to give a clear answer. *Recommended: one small control arm that costs no code:* skeleton-only folds, which the build has as its fallback (CM-20), on the same scenario and probes, one session per family. It takes about half a day of lab time and answers one question: whether enrichment helps at all. Add a plain-summary arm only if the main arm fails a probe: it needs about 200 lines behind a setting, and about a day of lab time for one session per family. With one or two sessions per arm, the results are anecdotes, not measurements, and should be read so.
5. **Fold proposals kept apart from the ledger** (CM-15). The alternative writes them into the ledger at once, as entries in their first status. *Recommended: kept apart until accepted.* A `proposed` decision in the ledger would read as an open choice, not as an abandoned approach, and an `assumption` would be `active` from the start; a proposal waiting in the digest misleads no one.
6. **Refusing an agent's change without provenance** (CM-9). The alternative accepts it and marks it. *Recommended: refuse.* A refusal teaches the agent at once, and the probes rely on anchors.
7. **The geometry's defaults** (CM-27): the trigger at 0.85 C, the wall at 0.90 C, a chunk and the raw floor at 0.2 C, and the budgets. *Recommended: as drafted,* tuned in the first lab round.
8. **Retrieval for delegated runs** (CM-37): their own run only. *Recommended: yes for now.* A task run could later search its session, if the labs show the need; a check run never.
9. **The enrichment model** (CM-19). *Recommended: the run's own alias,* which reads the run's cached view and knows the work; another alias is a setting, with no cache benefit.
10. **What a check may read with its own ledger tools.** CM-37a keeps the parent's conclusions out of a checker's view, but PD-34 offers a check the ledger's read tools (`ledger_read`, `ledger_list`, `ledger_history`, `ledger_work`). With them it can read other entries, such as a decision to establish its claim, and the work records of the entry it checks, which hold earlier checks' outcomes (LG-F8). The view's path is closed; the tools' path is not. *Recommended:* limit a check's ledger reads to the entry under check and what its brief and the entry's evidence pin, and leave out the entry's work records and the incoming links that lead to decisions. That is a change to LG-13 and PD-34 for the owner to decide, outside this spec's first build.
11. **Retiring failed tool calls** (CM-21a). A failed or timed-out call retires once its fold is merged into an overview, unless a work record cites it. *Recommended: yes;* a failure that matters belongs in a work record (CM-48), and the archive keeps every call.

## Decided by the owner (2026-10-15)

1. The approach must work for both Claude and GPT models. The owner uses both, and a session may switch between them (CM-2, CM-31, CM-42).
2. The project follows proven designs, Claude Science's and OpenScience's, rather than inventing its own. No context-management library is adopted, since none is known to be good for this workload.
3. The working context's ceiling is configurable per role, 600,000 tokens by default; 200,000 is too small. It is always capped by the model's window less its maximum output (CM-27). The default is the owner's choice, and the first lab round checks its effect on quality.
4. The spec is organized by layers. The research state is an explicitly kept record on the ledger and the plan, with provenance, kept current by the agent and proposed to by folds, never settled by them (CM-8 to CM-16).
5. The continuity probes, on both model families, are the first build's acceptance condition; a comparison against simpler approaches is an open question (open question 4).

## Sources

- The owner's study of context management for long research sessions (2026-10-15; kept outside the repository): its survey of libraries and memory systems, its reading of Claude Science, OpenScience, Codex, OpenCode and Claude Code, and its evidence review.
- **Claude Science** 0.1.60 (proprietary): rolling folds, the skeleton and its per-class budgets, the model-written connective text, the check of search keys, the placement of folds in the view, BM25 with exact-substring ranking, incomplete-read markers and stripping thinking when history is rewritten. This spec describes that behavior in its own words; no prompt, setting name or code is used, and its budgets are the project's own (D56).
- **OpenScience** ([synthetic-sciences/openscience](https://github.com/synthetic-sciences/openscience) at `722ad05`, Apache-2.0; its `NOTICE` reproduces OpenCode's MIT license): the breaker, rejecting empty or truncated summaries, updating rather than rewriting a summary, findings with their source and whether they were verified, and `recall` as a reference for history search. **OpenScience Desktop** ([ai4s-research/open-science](https://github.com/ai4s-research/open-science) at `0c88be0`, MIT), which runs OpenCode 1.18.32's compaction.
- **Codex** ([openai/codex](https://github.com/openai/codex), `rust-v0.163.0` at `6b99197`, Apache-2.0): history normalization, token accounting, the kept user messages, and compacting with the old model before a switch. **OpenCode** ([anomalyco/opencode](https://github.com/anomalyco/opencode), 1.19.1 and `dev` at `d7d44a7`, MIT): the overflow classifier, shrink-and-retry and the cross-model replay rules.
- **LiteLLM**: the owner's gateway runs 1.102.1; the thinking fixes are in `v1.107.0-dev.3`.
- Anthropic's documentation on preserved thinking, compaction, context editing and prompt caching; OpenAI's on reasoning, compaction and the Responses API; both providers' published prices, retrieved 2026-10-15.
- Evidence: Governance Decay (constraint pinning), ACON (literal-keeping summaries), the Complexity Trap (masking against summaries), MemoryArena and MemoryAgentBench (retrieval over originals), SelfCompact (task boundaries), DeLoop (repeated work after compaction), and Chroma's "Context Rot" and the Graphwalks results (reasoning that weakens as the context grows).
- Repository: [run service](run-service.md), [plans and delegation](plans-and-delegation.md), [ledger](ledger.md), [project memory](project-memory.md), [tools](tools.md), [PDF sources](pdf-sources.md), [orchestrator prompts](../ORCHESTRATOR-PROMPTS.md), [threat model](../THREAT-MODEL.md), [licensing](../LICENSING.md), [decisions](../DECISIONS.md) D56, D61, D72.

**Porting and notices.** Code PRs that port mechanics keep each source's license and copyright notice in the ported files and say what they changed. From an Apache-2.0 source (Codex, OpenScience), they also carry any `NOTICE` text the source holds at the ported commit; OpenScience's carries OpenCode's MIT notice. Each such PR adds its source to [licensing](../LICENSING.md), and checks each file it borrows from OpenScience against D56, since some OpenScience material derives from Claude Science text. Nothing is ported from Claude Science.
