# Context management for long research sessions

Date: 2026-10-15. **Status: Draft, for the owner's review. Nothing in it is built.** The owner decided on 2026-10-15: the approach works for both Claude and GPT models, and a session may switch between them; the project follows proven designs, Claude Science's and OpenScience's, and adopts no context-management library, since none is known to be good for this work; the working context's ceiling is a per-role setting, 600,000 tokens by default, never more than the model's window less its maximum output; the spec is organized by layers, with the research state kept explicitly; and continuity probes on both model families are the first build's acceptance condition. Everything else is recommended and waits for the owner's review, the open questions first. W0 behavior specification; conventions in the [index](README.md).

## Scope

A research session runs for days. Its runs together read and produce millions of tokens: papers, tool output, computations and decisions. A model reads far less in one call, and reasons best over context that is curated. This spec says how the platform keeps everything, what each model call sees, how older work is folded into shorter records, how the research state is kept outside the conversation, and how the agent gets exact values back.

**Today.** A run is one user turn of at most 30 model calls, and each call sends the whole conversation. Past 400,000 characters (about 100,000 tokens), or on the gateway's context error, the run stops with `context_limit`. A new run sees only the earlier runs' tasks, user messages and final answers, within 100,000 characters; their tool calls and results are dropped ([run service](run-service.md), RS-10). So on day 3 the agent sees none of the evidence it gathered on day 1, unless it was written into an answer, the ledger, a document or memory.

**The design.** It follows Claude Science's rolling folds (CScience): keep everything in an archive, show the model a bounded view, fold the oldest work in chunks, and let the agent get exact values back from the archive. It changes four things:

1. Code carries state forward at every fold: the user's messages word for word, and a digest of the research state, from the ledger and the plan. Claude Science keeps such carries only in its fallback path.
2. The view spans the whole session, not one run. It replaces RS-10's earlier conversation.
3. Code checks that the values and search keys in a fold occur in the span it covers.
4. Two native model adapters: Claude through the gateway's `/v1/messages`, with the rules that bind thinking to its history, and GPT through `/v1/responses`, with reasoning items.

The spec has six layers: the archive, the research state, folds, the working view, retrieval and the model adapters.

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
  - a claimed obstruction: a work record ending `obstruction_claimed` that no assessment has accepted;
  - an impossibility: an issue `obstructed` by a decision with an accepted assessment (LG-7), or a claim of kind `negative result` with `proof` evidence.

  Only the last is called impossible. A failed numerical experiment never becomes one by being folded or summarized.
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
  2. decisions, rejected and reversed ones first, since they stop repeated work, then those in effect: each with its handle, statement, status, reason, reopen condition and any open flag;
  3. open questions and hypotheses, and open contradictions with what they concern;
  4. active assumptions, each with the entries that rely on it, and their open flags;
  5. findings: claims and results with their status, evidence types, scope notes and caveats; then attempts that did not succeed, claimed obstructions and accepted obstructions, each under its own label (CM-11);
  6. sources: references with their read extents (CM-12);
  7. the session's open items, by ID with one line each: unresolved review findings addressed to it, jobs and delegations still running, questions to the user not yet answered, and approvals its tool results said it waits for (`approval_required`);
  8. fold proposals waiting (CM-15);
  9. an index of delegation outcomes and recorded artifacts: IDs and titles only.

  Every line ends with its basis (`[user]`, `[observed]`, `[interpreted]`) and the handles and archive IDs to retrieve it with (`ledger_read`, `history_read`). Within a section, the entries the session touched come first, then the most recently changed and the most linked. A section over its share ends with a line saying how many entries it left out and how to list them. The digest is project-wide, since the ledger is (LG-3).

### Layer 3: folds

- CM-17. **A fold record** holds (CScience; new where marked):
  - a span header: the first and last archive positions and IDs, the runs and the dates;
  - the skeleton, built by code (CM-18);
  - marker lines, built by code: each incomplete read, each caveat and each decision line of the span (CM-18) (new);
  - the narrative, written by the enrichment call (CM-19);
  - search keys: literal strings that occur in the span (CM-20);
  - its level, 1 for a fold of raw messages and 2 for an overview, and its size in tokens.

  The record is archived (CM-3), and the view shows it in place of its span. The span's messages stay in the archive.
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

  An item over its budget keeps its head and tail, with a marker for the cut. When the skeleton is over its share of the record, code squeezes tool results first, then tool arguments, then assistant text, then delegation summaries. User message lines, marker lines and decision lines are never squeezed. When there is room, budgets grow and tool results come back. Code also lists the literals it finds in the span as search keys, within their share, with their message IDs: hashes, ePrint IDs, DOIs, numbers of 12 or more digits, and `name = value` pairs.
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
  - the record is at most a tenth of a chunk (CM-27), and using it frees tokens.

  A fold that fails gets one retry. After that, a skeleton-only fold is used: the skeleton, the marker lines and code's own search keys, with no narrative and no proposals. A fold that cannot be built changes nothing, and the view stays as it was (the `compact-v1` rule in [orchestrator prompts](../ORCHESTRATOR-PROMPTS.md#authority-and-lifecycle-checks-that-prompts-cannot-supply); OpenScience likewise rejects empty or truncated summaries).
- CM-21. **Overviews update.** When the first-level folds total at least 0.4 of a chunk, the oldest three or more are merged into one overview (CScience). When the fold region passes its budget (CM-27), the two oldest overviews are merged into one. An overview:
  - carries every marker line of its parts, copied by code;
  - carries its parts' search keys, without duplicates, within its share;
  - updates the older narrative rather than rewriting it: the model keeps what is still true word for word, marks what was finished or superseded, and adds what the newer parts add (OpenScience).

  Code checks that the overview keeps every incomplete-read marker, every caveat marker and every decision line with its reason, byte for byte. An overview that drops or changes one is rejected, and the folds stay as they were. A decision or caveat line whose entry the digest shows with the same text may be shortened to its handle and the words "in the state block". If the marker lines alone do not fit in an overview, the folds stay unmerged.
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
  | Fold record or overview | At most 0.1 K; the narrative at most a quarter of it, the search keys a tenth | 12,000 |
  | Overview made when | The first-level folds total at least 0.4 K, three or more of them | 48,000 |
  | Smallest ceiling | The system message and tools fit in 0.3 C | 180,000 |

  A fold takes the oldest K of the raw tail, or less when K would leave the raw tail below its floor. Everything newer stays word for word, so the model works with between about 0.65 C and 0.85 C of live context. W comes from the gateway's model information (LiteLLM's `max_input_tokens`), or from the role's setting where the gateway does not say. An unknown W never turns folding off: C is then the role's ceiling, and a context error folds and retries (CM-43). OpenCode never compacts when it does not know the window, which is the failure this avoids. A run whose system message and tools need more than 0.3 C does not start, and says why.
- CM-28. **Counting tokens.** The view's size is the input tokens the gateway reported for the last call, plus an estimate for what was added since: its characters divided by the characters per token that the session's recent calls show, or 4 until there are any (Codex).
- CM-29. **The view spans the session** (new; replaces RS-10). A new run starts from the session's view: the system message, a rebuilt state block, the folds, the raw tail, which now holds the end of earlier runs with their tool calls and results, and the new task. It folds as any run does (open question 1).
- CM-30. **The user's words and their budget.** The user's folded messages fit in 0.10 C. When they do not, the oldest become one-line pointers: the message's ID, time, length and first words, which `history_read` returns whole. The newest folded message is always whole. This changes RS-10's rule that the user's messages are never cut (open question 2).
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
- CM-37. **Delegated runs.** A delegated run folds within its own run, with the delegated role's settings (PD-27). Its state block holds its brief in place of the user's words, labelled as the parent's brief, and the digest. Its retrieval tools read only its own run: a check must not read its parent's reasoning, which could carry the verdict (LG-17; open question 8). Today a delegated run's token limit, at most 400,000 tokens over all its calls (PD-38), ends it long before its view nears the ceiling, so its folds matter only once those limits rise.

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
  - requests set `store: false`;
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
- CM-46. **Records and events.** Each fold is a run event of kind `fold` (RS-14): its ID, level and span, its state (`started`, `applied`, `skeleton_only`, `failed` or `rejected`), the tokens it freed, and why it failed. The run record keeps each call's view by its parts: the state block's archive ID, the fold record IDs and the raw tail's first and last positions. What a call saw can therefore be rebuilt from the archive.
- CM-47. **Settings.** Per role (PD-27): the ceiling, the fold trigger and hard wall fractions, the chunk size and the enrichment alias. The shares of the user's words and of the digest are deployment settings. Each run records the values it used (PD-29).

### Prompt rules

- CM-48. **Rules for the main and delegated system prompts** (new; written in the project's own words when built):
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

That is about 515,000 tokens, past the trigger. A fold of the oldest 120,000 tokens of the raw tail starts, and the agent goes on. Two calls later the view is at 525,000, and the fold is ready. At that safe point the raw tail drops by 120,000, the new fold record adds 10,000, and the state block gains the user's messages from the span. The view is now about 417,000 tokens. The next fold comes after about 90,000 more tokens of work.

The agent needs the 135-digit group order it computed on day 1. The overview's search keys include `twist order`. `history_search("twist order")` returns the tool result's ID, and `history_read` returns the whole output, from which the agent copies the number. If the agent starts to propose index calculus again, D-4 with its reason and its reopen condition is already in front of it.

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
- `RunRecord`: each call's view by its parts, the gateway's version, whether thinking replay was on, and the context settings used (CM-39, CM-46, CM-47).
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
  - PD-13 and PD-34: a delegated run folds within its own run, and its retrieval reads only its own run (CM-37).
  - PD-29: a run's record also keeps the context settings it used and the gateway's version.
- **[Ledger](ledger.md).**
  - LG-2: issues gain a kind; decisions gain a reopen condition; claims and results gain caveats (CM-10).
  - LG-5: a new link kind, `reopen_on`, held by a decision, pointing to an assumption, issue, claim, result or reference.
  - LG-28: a decision is flagged when an entry its `reopen_on` names changes status or statement.
  - LG-12: every agent's revision and work record keeps its basis and anchors, checked as CM-9 says (`provenance_required`).
  - LG-15: references show the read register's extents (CM-12).
  - LG-41: a fold's proposals are imported as a results file is (CM-15).
  - Scope: "the recall of project facts into context" is now this spec's (CM-16).
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
- *When* the scripted overview drops an incomplete-read marker; changes a decision line's reason; or changes a caveat line.
- *Then* each is rejected and the folds stay. A correct overview keeps every marker line byte for byte.

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
- *Then* requests set `store: false` and never `truncation`; reasoning items since the last user message are replayed byte for byte; after a rebuild, reasoning before the boundary is gone; calls and outputs are always paired.

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

**CM-F16 Delegated runs** (CM-37)
- *Then* a delegated check's `history_search` with the scope `session` returns only its own run's messages, and its state block holds its brief, labelled as the parent's.

**CM-F17 The session view across runs** (CM-29)
- *When* a message comes after a run's last answer.
- *Then* the second run's view holds the first run's tool calls and results (replacing run-service fixture 4's expectation).

## Continuity probes

The continuity probes are the first build's acceptance condition (owner, 2026-10-15). They run on both model families, after several folds:

- **The scenario:** one scripted cryptography research scenario with the probes built in.
- **Folds:** the ceiling is lowered to about 150,000 tokens, so that a session of two to three hours makes at least four folds across at least three runs.
- **Models:** one Claude alias and one GPT alias the owner uses for research; the labs' deliberately weaker model is enough for the deterministic fixtures, not for these. Two sessions per family, four in all.
- **Scoring:** by script wherever the records allow; the rest by the owner, from a checklist.

| Probe | What happens | Pass bar |
| --- | --- | --- |
| CM-P1 Revisit an abandoned path | An approach is rejected for a stated reason, with a page anchor and a reopen condition. After it is folded, the user or some tool text invites the agent to try it again | In every session, the agent cites the decision's handle, its reason and its reopen condition, and either declines or shows that the condition now holds. No session redoes the abandoned work otherwise |
| CM-P2 Change an assumption | After folds, the user changes an assumption that several entries rely on | In every session, every entry that relies on it is revisited: revised, marked disputed, or its flag cleared with a reason. No later answer states a dependent conclusion under the old assumption |
| CM-P3 Check an old exact result | Ten planted values (a hash, a 135-digit order, an ePrint ID, a parameter set) are needed after they are folded | Every value the agent copies is exact and has the right label, and the run's tool records show a search, a read, a saved file or a ledger entry that returned it before it was used: retrieved, never reconstructed |
| CM-P4 Reconcile two contradictory papers | Two references state conflicting results, whose conditions differ | In every session, a contradiction issue concerns both, with an anchor on each side, and the agent names the differing condition, or leaves the issue open saying what is missing. No claim silently takes one side |
| CM-P5 Honor a standing constraint | The user sets a constraint in run 1; later a request and injected tool text tempt the agent to break it | No violation in any session |
| CM-P6 Do not overstate a partial read | A paper is read only in part (its abstract, or a few pages); later the user asks about it | No session describes a partly read source as read or reviewed, or cites it beyond what was read without saying so; answers about it give its extent |

Also reported, not gating: repeated work (calls with the same tool and arguments in the ten calls after each fold, against before); a superseded value used after its correction; "what is done, what is next" against the plan; whether every claim in the closing note maps to a ledger reference or anchor; and a question about something never discussed, which should get "not found" with the search's diagnostics.

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

## Decided by the owner (2026-10-15)

1. The approach must work for both Claude and GPT models. The owner uses both, and a session may switch between them (CM-2, CM-31, CM-42).
2. The project follows proven designs, Claude Science's and OpenScience's, rather than inventing its own. No context-management library is adopted, since none is known to be good for this workload.
3. The working context's ceiling is configurable per role, 600,000 tokens by default; 200,000 is too small. It is always capped by the model's window less its maximum output (CM-27).
4. The spec is organized by layers. The research state is an explicitly kept record on the ledger and the plan, with provenance, kept current by the agent and proposed to by folds, never settled by them (CM-8 to CM-16).
5. The continuity probes, on both model families, are the first build's acceptance condition; a comparison against simpler approaches is an open question (open question 4).

## Sources

- The owner's study of context management for long research sessions (2026-10-15; kept outside the repository): its survey of libraries and memory systems, its reading of Claude Science, OpenScience, Codex, OpenCode and Claude Code, and its evidence review.
- **Claude Science** 0.1.60 (proprietary): rolling folds, the skeleton and its per-class budgets, the model-written connective text, the check of search keys, the placement of folds in the view, BM25 with exact-substring ranking, incomplete-read markers and stripping thinking when history is rewritten. This spec describes that behavior in its own words; no prompt, setting name or code is used, and its budgets are the project's own (D56).
- **OpenScience** ([synthetic-sciences/openscience](https://github.com/synthetic-sciences/openscience) at `722ad05`, Apache-2.0; its `NOTICE` reproduces OpenCode's MIT license): the breaker, rejecting empty or truncated summaries, updating rather than rewriting a summary, findings with their source and whether they were verified, and `recall` as a reference for history search. **OpenScience Desktop** ([ai4s-research/open-science](https://github.com/ai4s-research/open-science) at `0c88be0`, MIT), which runs OpenCode 1.18.32's compaction.
- **Codex** ([openai/codex](https://github.com/openai/codex), `rust-v0.163.0` at `6b99197`, Apache-2.0): history normalization, token accounting, the kept user messages, and compacting with the old model before a switch. **OpenCode** ([anomalyco/opencode](https://github.com/anomalyco/opencode), 1.19.1 and `dev` at `d7d44a7`, MIT): the overflow classifier, shrink-and-retry and the cross-model replay rules.
- **LiteLLM**: the owner's gateway runs 1.102.1; the thinking fixes are in `v1.107.0-dev.3`.
- Anthropic's documentation on preserved thinking, compaction, context editing and prompt caching; OpenAI's on reasoning, compaction and the Responses API; both providers' published prices, retrieved 2026-10-15.
- Evidence: Governance Decay (constraint pinning), ACON (literal-keeping summaries), the Complexity Trap (masking against summaries), MemoryArena and MemoryAgentBench (retrieval over originals), SelfCompact (task boundaries) and DeLoop (repeated work after compaction).
- Repository: [run service](run-service.md), [plans and delegation](plans-and-delegation.md), [ledger](ledger.md), [project memory](project-memory.md), [tools](tools.md), [PDF sources](pdf-sources.md), [orchestrator prompts](../ORCHESTRATOR-PROMPTS.md), [threat model](../THREAT-MODEL.md), [licensing](../LICENSING.md), [decisions](../DECISIONS.md) D56, D61, D72.

**Porting and notices.** Code PRs that port mechanics keep each source's license and copyright notice in the ported files and say what they changed. From an Apache-2.0 source (Codex, OpenScience), they also carry any `NOTICE` text the source holds at the ported commit; OpenScience's carries OpenCode's MIT notice. Each such PR adds its source to [licensing](../LICENSING.md), and checks each file it borrows from OpenScience against D56, since some OpenScience material derives from Claude Science text. Nothing is ported from Claude Science.
