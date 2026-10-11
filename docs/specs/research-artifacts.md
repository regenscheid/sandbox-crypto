# Research artifacts

Date: 2026-10-11. **Status: Approved as drafted (2026-10-11, by the user), with RA-6 amended for the shared files' sync (D79), and RA-1 and RA-24 amended for living documents later the same day.** Open questions 1 and 3 are settled as proposed; the others are settled as the implementation reaches them, and recorded here. W0 behavior specification; conventions in the [index](README.md).

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

- RA-5. Executions write in the session workspace. The worker reports every file an execution creates, changes or deletes there ([tools](tools.md)); each one becomes a new revision of the artifact at its path, except paths matching the project's ignore patterns (caches, temporary and build directories) (new; see open question 1). Shell commands, kernel cells and helper code are treated alike.
- RA-6. Shared artifacts live in the project's `shared/` area, which the worker keeps in step across the project's environments ([worker](../WORKER.md#shared-data-across-environments), D79). *Amended 2026-10-11; the draft had shared artifacts change only through publication from a session.*
  - A change made in an environment's `shared/`, by an execution or a file request, becomes a revision of the shared artifact when the worker syncs it after the call.
  - A write that names the expected current revision (a file request, or publication of a session revision to a shared path) gets a conflict naming the current revision if another arrived meanwhile; nothing is overwritten (D37).
  - When two environments changed the same file between syncs, the change that arrived first keeps the path, and the later one is kept beside it as a conflict copy named after its environment; the agent whose change came second is told (D79).
- RA-7. Imports create revisions with import attribution instead of an execution: user uploads, connector and MCP results (binary results become artifacts in the MCP proxy, D67), and fetched sources. The attribution records the source (URL, DOI, or server, tool and an argument hash), retrieval time, importing actor and response metadata.
- RA-8. Control-plane content (documents, plans, review reports) is written through `Mutation`s that name the expected revision ([plans and delegation](plans-and-delegation.md); research workspace for documents).
- RA-9. Writing or publishing content identical to the current revision creates no new revision; it adds a `reproduced_by` link from the existing revision to the new attempt (new).
- RA-10. The repository verifies the content hash on every transfer. A mismatch fails the publication, records the error and creates no revision (new).
- RA-11. Files written by a failed or cancelled attempt are still recorded, and their lineage carries the attempt's outcome (`failed`, `cancelled`), so partial output is visible and distinguishable ([architecture](../ARCHITECTURE.md), execution history).
- RA-12. Symbolic links in a workspace are recorded as links and never followed; the orchestrator never mounts workspaces, and it treats artifact content as untrusted input, with size and type limits and no in-process parsing or rendering of rich formats (architecture).

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

- RA-19. A reference to an artifact is `pinned` to a revision or follows the `latest` revision. Evidence (claims, findings, lineage, ledger entries, delegation outcomes) MUST use pinned references. Presentation links, such as a figure embedded in a living document, may follow the latest revision and show which revision they resolved to (CScience distinguishes the two; D28, [product](../PRODUCT.md)).
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
6. Renames: detect a move when a deleted file's content reappears at another path in the same execution, or treat it as a new artifact?

## Sources

[Feature plan](../FEATURE-PLAN.md) (provenance), [research workspace](../RESEARCH-WORKSPACE.md), [projects and sessions](../PROJECTS-AND-SESSIONS.md) (data sharing), [architecture](../ARCHITECTURE.md) (storage, execution history), [implementation plan](../IMPLEMENTATION-PLAN.md) (shared contracts), [MCP connectors](../MCP-CONNECTORS.md), decisions D09, D10, D23, D25, D28, D29, D37, D40, D67. Behavior evidence: the [Claude Science](../../.agents/docs/agent-reports/claude-science.md) report (artifact versions, lineage, annotations, pinned and live references) and the [OpenScience Desktop](../../.agents/docs/agent-reports/openscience-desktop.md) report with its pinned `provenance.rs` and `runs.ts` (MIT).
