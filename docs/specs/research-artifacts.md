# Research artifacts

Date: 2026-10-11. **Status: Approved as drafted (2026-10-11, by the user), with RA-6 amended for the shared files' sync (D79), and RA-1 and RA-24 amended for living documents later the same day. RA-27 to RA-39, [files from executions and delegated runs](#files-from-executions-and-delegated-runs), were approved by the owner on 2026-10-14 (open questions 6 to 14). Built for tool calls ([artifacts](../ARTIFACTS.md#recording-the-files-calls-write)): RA-27 to RA-35 and RA-39, background jobs' files included, and RA-37's artifact list; RA-37's artifact history and a run's page in the run tree are not built, and RA-36 is built for delegated runs, and RA-38 for delegated tasks. RA-9 and RA-37 were amended on 2026-10-15 (lab 38, B7): publishing a file unchanged makes it a result.** Open questions 1 and 3 are settled as proposed; the others are settled as the implementation reaches them, and recorded here. W0 behavior specification; conventions in the [index](README.md). As built: [immutable artifact inventory](../ARTIFACTS.md) (explicit publication only).

## Scope

Research artifacts are the files and records a project produces or imports: execution outputs, figures, tables, data, imported sources, documents and plans. This spec covers their identity and revisions, how revisions are created and published, lineage (producing execution, inputs, environment), references to them, and annotations. CScience already versions artifacts and records lineage (CScience); this spec keeps that behavior on the worker boundary, with the improvements the plan asks for (D09, D10, D28, D37, D40).

Out of scope: editing semantics of living documents and their block anchors ([research workspace](../RESEARCH-WORKSPACE.md), D65); the ledger and board; PDF extraction and page anchors (a later spec); how files move between worker and repository ([tools](tools.md), file handoff).

Terms:

- **Artifact**: a project-scoped resource with a stable ID and an append-only sequence of immutable revisions.
- **Revision**: one version of an artifact: content addressed by SHA-256, media type, size, revision number, parent revisions and attribution.
- **Lineage**: how a revision came to be: the producing execution, its inputs and environment, or an import or user action.
- **Annotation**: a comment or note attached to an anchored location in one revision, with replies.

## Behavior

### Identity and revisions

- RA-1. Every artifact has a stable ID and a project-scoped path (`shared/...` or `sessions/<session>/...`, [projects and sessions](../PROJECTS-AND-SESSIONS.md); living documents under `documents/...`). Revisions are append-only; none is changed in place (CScience; D28). *Amended 2026-10-11: living documents have a third area, `documents/`, whose files the platform keeps at each document's current revision in every environment, while their history stays in the control store ([living documents](living-documents.md), LD-3, LD-49).*
- RA-2. A revision records the SHA-256 of its content, size, media type, revision number, parent revisions, creation time and attribution. Hashes MUST be cryptographic (OpenScience Desktop's 64-bit hashes are not).
- RA-3. Deleting a file adds a tombstone revision; history remains. Purging content is a separate action that only the user can take, and it keeps the revision records (new).
- RA-4. Content of large revisions can be dropped under a retention setting. The record (hash, size, lineage) stays and is marked `content_dropped` (new).

### Creating revisions

- RA-5. Executions write in the session workspace. The worker reports every file an execution creates, changes or deletes there ([tools](tools.md)); each one becomes a new revision of the artifact at its path, except paths matching the project's ignore patterns (caches, temporary and build directories) (new; see open question 1). Shell commands, kernel cells and helper code are treated alike. How, in detail: [files from executions and delegated runs](#files-from-executions-and-delegated-runs) (proposed).
- RA-6. Shared artifacts live in the project's `shared/` area, which the worker keeps in step across the project's environments ([worker](../WORKER.md#shared-data-across-environments), D79). *Amended 2026-10-11; the draft had shared artifacts change only through publication from a session.*
  - A change made in an environment's `shared/`, by an execution or a file request, becomes a revision of the shared artifact when the worker syncs it after the call.
  - A write that names the expected current revision (a file request, or publication of a session revision to a shared path) gets a conflict naming the current revision if another arrived meanwhile; nothing is overwritten (D37).
  - When two environments changed the same file between syncs, the change that arrived first keeps the path, and the later one is kept beside it as a conflict copy named after its environment; the agent whose change came second is told (D79).
- RA-7. Imports create revisions with import attribution instead of an execution: user uploads, connector and MCP results (binary results become artifacts in the MCP proxy, D67), and fetched sources. The attribution records the source (URL, DOI, or server, tool and an argument hash), retrieval time, importing actor and response metadata.
- RA-8. Control-plane content (documents, plans, review reports) is written through `Mutation`s that name the expected revision ([plans and delegation](plans-and-delegation.md); research workspace for documents).
- RA-9. Writing or publishing content identical to the current revision creates no new revision; it adds a `reproduced_by` link from the existing revision to the new attempt (new). A publication or a declared result that finds the same content still makes the artifact a result (RA-37): it pins the existing revision, and its receipt says the content was reproduced. So a file a call wrote (RA-27) and the agent then published unchanged is a result, at the one revision that holds those bytes. A call that writes the same bytes again makes nothing a result.
- RA-10. The repository verifies the content hash on every transfer. A mismatch fails the publication, records the error and creates no revision (new).
- RA-11. Files written by a failed or cancelled attempt are still recorded, and their lineage carries the attempt's outcome (`failed`, `cancelled`), so partial output is visible and distinguishable ([architecture](../ARCHITECTURE.md), execution history).
- RA-12. Symbolic links in a workspace are recorded as links and never followed; the orchestrator never mounts workspaces, and it treats artifact content as untrusted input, with size and type limits and no in-process parsing or rendering of rich formats (architecture).

### Files from executions and delegated runs

*Approved by the owner on 2026-10-14, with the recommendations of open questions 6 to 13 and the owner's answer to 14; not built. It details RA-3, RA-4, RA-5 and RA-11, and amends PD-17 of [plans and delegation](plans-and-delegation.md).*

**What is built today.** After every shell call, kernel cell and helper call, the worker reports the files the call created or changed in the session workspace and in `shared/`: up to 1,000 paths, found by comparing each file's size, modification time and inode before and after the call ([tools](tools.md); `written_files`). The list goes to the model and, as names only, to the consistency reviewer; no file becomes an artifact from it. A file becomes an artifact only when the agent calls `publish` on it: its content, up to 8 MiB, is copied into the control store as a revision, attributed to the run and the call ([artifact inventory](../ARTIFACTS.md)). The GUI lists published artifacts at their latest revision, and opens PDFs in the viewer. Deleted files are not reported, and nothing records a deletion.

**What this adds.** A delegated run names the files that are its results, and its parent receives them as pinned artifact revisions. Every other file a call writes becomes an artifact revision by itself, without `publish`, unless the project chose to record results only; a deleted file gets a tombstone; and a delegated run's outcome lists all the files its calls wrote.

- RA-27. **Which files.** In a project that records all files, the default (open question 14), when a call ends (a shell call, a kernel cell, a helper such as a PDF render, or `write_file`), the harness records each file the call created or changed in the session workspace or in `shared/` as a new revision of the artifact at that path, before the call's result goes back to the model. A path under `shared/` is the shared artifact `shared/<path>`; any other path is `sessions/<session>/<path>`. Files matching the project's ignore patterns (RA-29) are left out. Content identical to the artifact's current revision adds no revision, only a `reproduced_by` link (RA-9). A background job's files are recorded when the harness sees the job end, attributed to the call that started it. Calls that failed, timed out or were cancelled are recorded too, labeled with that outcome (RA-11).
- RA-28. **Only when published.** In a project that records results only, a call's files become artifacts only when they are published or are a delegated run's declared results (RA-38). In either setting, files matching an ignore pattern, files the per-call bound left out (RA-30) and files outside the session workspace and `shared/` (such as `/tmp`) become artifacts only when the agent or the user publishes them. `publish` works as today, and overrides the ignore patterns for the file it names.
- RA-29. **Ignore patterns.** Each project has a list of ignore patterns, which the user edits in the project's settings. A pattern is matched against the path inside its area (the session workspace or `shared/`), with the rules of `.gitignore`: `*` matches within a name, `**` across directories, a trailing `/` matches a directory and everything in it. A new project starts with the defaults: `.jobs/` (background jobs' logs), `.cache/`, `__pycache__/`, `*.pyc`, `.ipynb_checkpoints/`, `.git/`, `.venv/`, `venv/`, `node_modules/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`, `build/`, `dist/`, `*.egg-info/`, `tmp/`, `*.tmp`, `*.swp`, `*~` and `.DS_Store`; and, once the platform keeps living documents' files in environments (LD-49), `documents/`, whose changes go to the documents themselves, with their own history (LD-50).
- RA-30. **Bounds.** One call records at most 100 files and 64 MiB of content. Beyond either, the rest of its files are not recorded, and the call's record and the GUI say how many were left out; the agent can publish the ones it needs. When the worker's own report was cut short (more than 1,000 changed files, or a workspace too large to compare), the call's record says the list is incomplete.
- RA-31. **Large files.** A revision keeps its content only up to 8 MiB. A larger file gets a revision with its SHA-256, its size and `content_retained: false`: the record, its hash and its lineage are kept, and the bytes stay only in the environment, where the agent and the user can still read them as long as the file is not changed. Files are hashed up to 4 GiB; a larger one is recorded as a gap on the call ("not recorded: larger than 4 GiB"), with its size. Dropping the content of old revisions to save space is RA-4's separate retention setting (open question 2).
- RA-32. **Space.** Automatic revisions count against the project's retention budget, as published ones do. When the project's content budget is full, new revisions keep their hash and size without content (`content_retained: false`, reason `quota`); when its revision count is full, no further revisions are recorded, every call says so, and the GUI shows the project's storage as full, until the user raises the budget or a retention setting frees space. Nothing is deleted to make room.
- RA-33. **Deletions.** The worker also reports the files a call deleted, when it could compare the whole workspace before and after the call. A deleted file that is an artifact gets a tombstone revision (RA-3); its earlier revisions stay. A file that was never recorded, or is ignored, gets nothing. A rename is a deletion and a new file: the old path gets a tombstone, the new path a new artifact (open question 6, as recommended).
- RA-34. **Attribution.** A revision recorded from a call names the run that made the call, its role (`main` or `delegated`) and, for a delegated run, its parent run and delegation; the tool call and its execution and attempt, with the attempt's outcome; and the model alias and, when the gateway reports it, the resolved model (RA-16). It says that the worker *reported* the file as written while the call ran, which is not proof that the call wrote it: a job or a kernel running at the same time, or another session for `shared/`, may have written it, and the bytes are those found when the harness copied the file right after the call. No automatic revision is ever attributed to the user.
- RA-35. **Links and special files.** Symbolic links are never followed, and are not recorded until the worker reports them as links (RA-12); sockets, pipes and devices are never recorded.
- RA-36. **Delegated runs.** A delegated run works in the session's workspace (PD-14), so its files are recorded like the main run's, attributed to it (RA-34). Its outcome (PD-17) gains `files`, an observation of the platform separate from the child's own result: every artifact revision and tombstone its calls recorded, as pinned references with path, revision, size, whether the content was kept, and the calls' gaps; at most 200 entries, with the total. The parent receives the list with the outcome (PD-35), after the declared results (RA-38); it reads the files by their paths in the shared workspace, or cites the pinned revisions as evidence (RA-19).

  *Built 2026-10-15:* a delegated run's record keeps, under `files`, its calls' recorded revisions and tombstones (path, artifact ID, revision, SHA-256, size, whether the content was kept, whether it was a tombstone), at most 200 with their `total`, and its calls' `gaps` by reason. Its delegation's outcome keeps the same list (`files`, control store version 38), apart from the child's result, written once; the table keeps at most 128 KiB of it, cut from the end with the total kept. The parent receives it after the result, the list cut to fit the answer (`cut`) with its total kept, and a notice gives its count (`files_recorded`).
- RA-37. **In the GUI.** The artifact list shows first the files that are results: delegated runs' declared results and published files, at their latest revision, with who declared or published them. A file published with the bytes it already had is a result too (RA-9); its latest revision is still the one the call recorded, and the list names that call as its writer. Under **All files** it shows every other recorded artifact, the same way: its path and area, its size, whether its content was kept, whether it was deleted, and who wrote it (the run, its role and the call). Both can be filtered by session and by run. A run's page in the run tree (PD-42) lists its declared results, then the files its calls recorded and the gaps. An artifact's history lists its revisions and tombstones.
- RA-38. **A delegated run's results.** A subagent's results often are files. Its outcome (PD-17) names them: `result_files`, a list of paths in the session workspace or `shared/`, at most 50. When the child returns, the platform checks each one: it must exist, be a regular file (not a link) and lie in the session workspace or `shared/`. It records each as an artifact revision pinned by its SHA-256, even when the file matches an ignore pattern or the project records results only, with the size rules of RA-31 (content kept up to 8 MiB; a larger file keeps its hash and size). Content identical to the artifact's current revision pins that revision (RA-9). A path that fails the check is listed with the reason (missing, a link, outside the workspace, larger than 4 GiB), and the harness asks the child once to correct its outcome, as for an invalid result (PD-18). The parent receives the results in the handback notice (PD-35) as references (path, artifact ID and revision, SHA-256, size, and whether the content was kept), separately from the platform's list of every file written (RA-36). The bytes are those found when the child returned; a later change to the file makes a new revision and leaves the pinned one as it was.

  *Built 2026-10-15, for delegated tasks, as the coordinator decided (reversible by the owner):* a task's result (`sandbox-task-result-2`) names its `result_files`, at most 50, each once and checked as agent text. The platform checks each against the revisions the child's own calls made: the latest a call recorded at that path (RA-27), or the one a `publish` made, which is how a project that records results only (RA-39), or a file an ignore pattern leaves out, gets a result. A file the child's calls did not make, or whose latest such revision is a tombstone, is refused: the answer gets the one correction of PD-18. The platform does not record a declared file itself. Each accepted file is pinned in the outcome (path, artifact ID, revision, SHA-256, size, whether the content was kept; kept with the delegation's `files` as `results`), and the parent receives them as `result_files` after the result and before the list of every file (RA-36), cut to fit its answer if need be; a notice names them. A task on an issue cites those whose content was kept on its outcome's work record (LG-F8). Checks name no result files.
- RA-39. **The project's setting.** Each project records either all files (the default: RA-27 with its ignore patterns, bounds and budget) or results only (declared results and published files). The user changes it in the project's settings; the change applies to calls that end afterwards.

#### A worked example

The main agent of session `s1` delegates "fetch and summarize three papers". The child run works in `s1`'s workspace and makes these calls:

| Child's call | Files the worker reports | What is recorded |
| --- | --- | --- |
| 1. `pip download` a PDF tool | `.cache/pip/…` (300 files) | Nothing: `.cache/` is ignored. |
| 2. `curl` two papers | `papers/smith2024.pdf` (2 MB), `papers/lee2023.pdf` (3 MB) | Two artifacts, `sessions/s1/papers/smith2024.pdf` and `…/lee2023.pdf`, revision 1 each, with content. The third paper's server refused; nothing to record. |
| 3. `curl` a dataset | `data/raw.h5` (2 GB) | `sessions/s1/data/raw.h5` revision 1, with its SHA-256 and size, `content_retained: false`: the 2 GB stay in the environment only. |
| 4. Python cell | `notes/summary.md`, `data/results.csv`, `notes/draft.md` | Three artifacts, revision 1 each, with content. |
| 5. Python cell | `shared/bibliography.bib` (changed), `notes/draft.md` deleted | `shared/bibliography.bib` gets a new revision, attributed to the child's call; `notes/draft.md` gets a tombstone, and its revision 1 stays. |
| 6. Returns its outcome, naming `notes/summary.md` and `data/results.csv` as its result files | | The platform checks both files and pins them: the revisions recorded in call 4, since their content has not changed. |

Each of these revisions names the child run, its role `delegated`, its parent (the main run) and its delegation, and the call during which the worker saw the file written.

**What the parent sees.** The handback notice holds the child's own result, as the child wrote it (status `partial`: one paper was unreachable; a summary); then its two result files as references: `notes/summary.md` and `data/results.csv`, each with its artifact revision, SHA-256 and size; then, separately, `files`: the seven revisions and one tombstone above, with `raw.h5` marked as not kept. The parent reads the two results from the workspace and cites their pinned revisions in its answer.

**What the user sees.** The artifact list for `s1` shows first the results, `notes/summary.md` and `data/results.csv`, "declared by the delegated run". Under **All files** it shows `papers/smith2024.pdf`, `papers/lee2023.pdf`, `data/raw.h5` (2 GB, content not kept), `notes/draft.md` (deleted) and, under the project's shared files, `bibliography.bib`, each with "delegated run, call N". The run tree shows the child run under the main run, with its results and the files its calls recorded. Opening `smith2024.pdf` opens the viewer. Nothing under `.cache/` appears anywhere.

**If the child had called `publish` on `summary.md`** after call 4, the publication would find the same content and add only a `reproduced_by` link (RA-9); the file stays a result, at the revision recorded in call 4.

**If the project recorded results only,** calls 1 to 5 would record nothing, and the two declared results would still be recorded and pinned when the child returned; the parent's `files` list would be empty, and the GUI's **All files** too.

### Lineage

- RA-13. A revision created by an execution records ([feature plan](../FEATURE-PLAN.md), provenance; D09, D10):
  - the producing attempt's `ExecutionContext`: project, session, environment and revision, kernel, execution, attempt, lease generation, policy revision and actor;
  - the code: cell source, or argv and working directory, with script files as pinned references;
  - the environment: base image digest, platform, isolation tier, and worker and pool identity;
  - the agent run and tool call that requested the execution, the skill revisions loaded in that run and any skill helper files used ([skills](skills.md));
  - inputs as pinned references, labeled `declared` (staged or named in the tool call) or `observed` (reported by trusted telemetry) (new).
- RA-14. For a kernel cell, lineage includes the chain of cells run in that kernel since it last started, because the result can depend on any of them (CScience ties versions to producing cells; the chain is new).
- RA-15. Lineage states what is known and labels the rest. File reads by processes are not observed by default, so lineage without observed inputs says so; it never presents inferred inputs as observed (new).
- RA-16. User uploads and edits are attributed to the user; agent writes to the agent run, with its role and resolved model. An agent MUST NOT create a revision attributed to the user (D23; OpenScience Desktop records an app-wide default model instead of the model that wrote).
- RA-17. A revision containing synthetic or simulated data carries the `synthetic` label, set by the producing call or the user; revisions derived from it show a synthetic input in their lineage (D29).
- RA-18. Lineage is a graph over revisions. From any revision the API and GUI navigate upstream to producing executions and inputs, and downstream to revisions, findings and documents that use it (new).

### References

- RA-19. A reference to an artifact is `pinned` to a revision or follows the `latest` revision. Evidence (claims, findings, lineage, ledger entries, delegation outcomes) MUST use pinned references. Presentation links, such as a figure embedded in a living document, may follow the latest revision and show which revision they resolved to (CScience distinguishes the two; D28, [product](../PRODUCT.md)). *Amended 2026-10-14 by the owner ([figures](figures.md), FG-7):* a reference to a recorded file written in chat, a living document or a ledger statement is pinned when its text is saved, so RA-F4's embedded file is pinned too, and the document shows that a newer revision exists.
- RA-20. References resolve in project scope; a reference into another project does not resolve without an explicit grant.

### Annotations

- RA-21. Users and agents can annotate one revision. An annotation has an `Anchor` (a text range with its quote and surrounding context, lines, a table cell, a notebook cell or output, an image region, or a PDF page and region), a body in Markdown with LaTeX (D25), its author, times, replies and an `open` or `resolved` status (CScience anchors include points, selections, regions, page numbers and quoted context).
- RA-22. An annotation stays attached to its revision. For each newer revision the platform tries to carry it forward, by block ID for documents or by quote and context otherwise, and reports the result: `current`, `moved`, `stale` (the anchored text changed) or `orphaned` (it is gone). Uncertain reattachment is visible ([research workspace](../RESEARCH-WORKSPACE.md)).
- RA-23. Annotations live in the control store, not in the artifact's content; exports can carry them alongside (new).
- RA-24. An annotation the user addresses to an agent becomes a scoped work request, and the agent's reply is attributed to its run (research workspace). *Amended 2026-10-11: an agent's annotation addressed to an agent starts no work, so that agents cannot keep waking each other ([living documents](living-documents.md), LD-22).*
- RA-25. Editing or resolving an annotation names the thread revision it expects; concurrent changes conflict instead of overwriting.

### Events

- RA-26. Each revision and annotation change is published as an `Event` committed together with the record (implementation plan, transactional outbox). Review triggers, projections and the GUI consume these events, not agent narration ([consistency review](consistency-review.md), CR-9).

## Contracts used

- `ArtifactRef`: stable ID, revision and SHA-256, media type, producing attempt or import attribution; the `pinned` or `latest` mode is a proposed addition ([index](README.md#proposed-contract-additions-new)).
- `ExecutionContext`: the producing attempt in lineage, with the actor and origin additions proposed in the index.
- `EnvironmentRecord`: environment revision, base image digest, platform and isolation tier in lineage.
- `Anchor`: annotation locations, carry-forward and the `stale` and `orphaned` states; image regions are a proposed addition.
- `Mutation`: shared publication and annotation changes name the expected revision; a conflict returns the current one.
- `Event`: `artifact.revision_created` with a lineage summary, `artifact.publication_conflicted`, `artifact.conflict_copied` (a sync kept a conflict copy, RA-6), `annotation.created`, `annotation.changed` and `annotation.reanchored`, produced by the repository and the worker.
- `PermissionRequest`: not used directly. Publication inside the project follows the project grant without per-file approval ([projects and sessions](../PROJECTS-AND-SESSIONS.md)).

## Acceptance fixtures

**RA-F1 Lineage of a kernel result** (`scripted`, `worker`; RA-5, RA-13, RA-14)
- *Given* a session with a Python kernel.
- *When* cell 1 sets `p = 17`; cell 2 runs `finite-field-identity-v1` over GF(p) and writes `results/ff.json`.
- *Then* one revision r1 of `results/ff.json` exists with the file's SHA-256 and the fixture's expected canonical JSON. Its lineage names cell 2's attempt, the kernel chain (cell 1, cell 2), the environment, revision, image digest and isolation tier, and the agent run, tool call and skill trace.

**RA-F2 Shell-written file** (`worker`; RA-5)
- *When* the agent runs `sh -c 'mkdir -p out && echo 42 > out/n.txt'`.
- *Then* `out/n.txt` has a revision whose lineage holds the argv and working directory, without any publish call, and its event reaches the review trigger ([consistency review](consistency-review.md), CR-F6).

**RA-F3 Same content, new content** (`worker`; RA-9)
- *When* cell 2 of RA-F1 runs again, then a changed cell writes different content.
- *Then* the rerun adds a `reproduced_by` link to r1 and no revision; the change creates r2 with parent r1.

**RA-F4 Pinned and latest** (`scripted`; RA-19)
- *Given* a document embedding `results/ff.json` as `latest` and a finding citing it pinned to r1.
- *When* r2 appears.
- *Then* the document resolves r2 and shows "r2"; the finding still resolves r1.

**RA-F5 Shared write conflicts** (`worker`; RA-6)
- *Given* sessions A and B in different environments of one project, both of which read shared `results/table.csv` at r3.
- *When* A writes it expecting r3, then B writes it expecting r3. Later, calls in A's and B's environments both change it before either syncs.
- *Then* A's write creates r4, and B's gets a conflict naming r4; nothing is overwritten. Of the two later changes, the first to sync becomes r5, the other is kept as `results/table.conflict-<B's environment>.csv`, and B's call is told.

**RA-F6 Import through a connector** (`worker`; RA-7)
- *When* the agent retrieves a PDF with a crypto-research-mcp tool through the MCP proxy.
- *Then* the PDF is an artifact revision with import attribution (server, tool, argument hash, retrieval time) and no producing execution; the model context holds a reference, not the bytes.

**RA-F7 Annotation carry-forward** (`scripted`; RA-21, RA-22)
- *Given* an annotation on the sentence "$a^{16} = 1$ for every nonzero $a$" in `report.md` r1.
- *When* r2 edits another paragraph, r3 rewrites that sentence, and r4 deletes its section.
- *Then* the annotation is `current` on r2, `stale` on r3 with the original quote shown, and `orphaned` on r4; it remains attached to r1 throughout.

**RA-F8 Partial output of a failed attempt** (`worker`; RA-11)
- *When* a cell writes `partial.csv` and then raises an exception.
- *Then* `partial.csv` has a revision whose lineage carries the outcome `failed`, and the API and GUI show it as partial.

**RA-F9 Attribution** (`scripted`; RA-16)
- *When* the user uploads `params.json` (r1), and the agent edits it (r2), then attempts to write r3 attributed to the user.
- *Then* r1 names the user and r2 the agent run with its resolved model; the third write is refused.

**RA-F10 Integrity and links** (`worker`; RA-10, RA-12)
- *When* a transfer's bytes are altered by fault injection after hashing, and the workspace contains a symbolic link to `/etc/passwd`.
- *Then* the altered publication fails and creates no revision; the link is recorded as a link with its target path, and no revision contains the target's content.

**RA-F11 Same result on two workers** (`worker`, phase 2; RA-13)
- *When* RA-F1 runs on babbage and on a cluster worker.
- *Then* both revisions have the same SHA-256, and their lineages differ in worker, pool, platform and isolation tier ([roadmap](../ROADMAP.md#first-end-to-end-fixture)).

**RA-F12 Synthetic data** (`scripted`; RA-17)
- *When* a helper writes simulated data to `sim.csv` with the `synthetic` label, and a plot is made from it.
- *Then* the plot's lineage shows a synthetic input.

## Open questions

1. **Settled (2026-10-11):** every file an execution writes in the session workspace, minus ignore patterns, becomes an artifact automatically (RA-5); not only declared output locations.
2. Retention: which size threshold, if any, should drop the content of old revisions (RA-4)?
3. **Settled (2026-10-11):** identical content written again adds a `reproduced_by` link instead of a revision (RA-9).
4. Observed inputs from monitor telemetry (RA-13): worth adding later, or are declared inputs and kernel chains enough?
5. Should exports include annotations, and in what form?
6. Renames: detect a move when a deleted file's content reappears at another path in the same execution, or treat it as a new artifact? *Decided by the owner (2026-10-14), as proposed (RA-33):* a tombstone at the old path and a new artifact at the new one; detecting moves can come later without changing either record.

The owner decided these on 2026-10-14 for [files from executions and delegated runs](#files-from-executions-and-delegated-runs), approving each recommendation:

7. **What is recorded, and when.** *Decided, as recommended:* every file a call writes in the session workspace or `shared/` (RA-27), including `write_file`, recorded before the call's result goes back to the model, so the run's next call cannot change the file first. That adds the copying time to each call, bounded by RA-30. The alternative, recording in the background, adds no delay but can record bytes a later call wrote.
8. **Bounds per call and per file.** *Decided, as recommended:* 100 files and 64 MiB of content per call; content kept up to 8 MiB a file, as for `publish` today; files hashed up to 4 GiB (RA-30, RA-31). A 2 GB file is then recorded with its hash and size, and its content stays only in the environment.
9. **Space.** The project's retention budget today is 64 MiB of content and 4,096 revisions, sized for explicit publication; automatic recording fills it quickly. *Decided, as recommended:* raise it to 2 GiB and 50,000 revisions per project (8 GiB and 200,000 overall), and when it is full keep recording hashes without content, then stop with a visible message, never deleting anything (RA-32).
10. **Ignore patterns.** *Decided, as recommended:* `.gitignore` rules, one list per project, edited in the project's settings in the GUI and by the command line, starting with the defaults in RA-29. The alternative is a fixed list with no settings.
11. **Deletions.** The worker does not report deleted files today. *Decided, as recommended:* it reports them when it compared the whole workspace (RA-33). This changes what the worker sends, so the worker protocol version goes up ([lockstep releases](../COMPATIBILITY.md)).
12. **What attribution claims.** *Decided, as recommended:* a revision says the worker reported the file as written during the call, and that this is not proof that the call wrote it (RA-34), as published artifacts already say for their worker hint. Proof of the writing process needs telemetry the platform does not have (open question 4).
13. **A delegated run's files** (PD-17). *Decided, as recommended:* the outcome gains `files`, the platform's own list of the revisions and tombstones the child's calls recorded, at most 200 entries with the total, kept apart from the child's own result (RA-36). The owner added declared result files (RA-38).
14. **Record everything, or only results?** *Decided by the owner (2026-10-14):* record all written files automatically by default, with the ignore patterns, bounds and budget above; declared results are always recorded and pinned; the GUI shows results and published files first and the rest under **All files**; "results only" is a project setting (RA-39).

## Sources

[Feature plan](../FEATURE-PLAN.md) (provenance), [research workspace](../RESEARCH-WORKSPACE.md), [projects and sessions](../PROJECTS-AND-SESSIONS.md) (data sharing), [architecture](../ARCHITECTURE.md) (storage, execution history), [implementation plan](../IMPLEMENTATION-PLAN.md) (shared contracts), [MCP connectors](../MCP-CONNECTORS.md), decisions D09, D10, D23, D25, D28, D29, D37, D40, D67. Behavior evidence: the [Claude Science](../../.agents/docs/agent-reports/claude-science.md) report (artifact versions, lineage, annotations, pinned and live references) and the [OpenScience Desktop](../../.agents/docs/agent-reports/openscience-desktop.md) report with its pinned `provenance.rs` and `runs.ts` (MIT).


### Durable large content (2026-10-17 amendment)

The owner requested artifacts independent of agent containers. New retained content
uses the control application's adjacent content-addressed filesystem store; a PVC
can mount the same directory. S3 is future work. Limits are now 1 GiB/file, 2 GiB
retained/call, 16 GiB/project and 64 GiB/store. Earlier 8 MiB/64 MiB limits above
describe the prior increment. Existing SQLite content is still readable. Per-call
exhaustion records hash/size metadata rather than dropping the file entirely.

See [durable artifact blobs](durable-artifact-blobs.md) for streaming ingestion,
durable reservations, publication crash boundaries and backup/cleanup requirements.
General authenticated pinned downloads stream independently of preview/model limits.
