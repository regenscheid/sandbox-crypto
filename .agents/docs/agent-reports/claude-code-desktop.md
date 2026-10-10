# Claude Code and Claude Desktop: research-support patterns

**Dated source-inspection report: 2026-10-10.** This is a working analysis of official documentation and public repository assets. It is not a record of implemented features or final project decisions. Later confirmed requirements and architecture choices remain in the current planning documents.

## Scope and evidence limits

The investigation examined publicly documented Claude Code, Claude Desktop, Claude Docs, and related research workflows. It used official Anthropic/Claude documentation and the official `anthropics/claude-code` and `anthropics/skills` repositories. No installed application state, private conversations, credentials, or private prompts were read. No runtime was installed, executed, or modified.

The strongest reusable patterns are structured skills, independent review, persistent shared knowledge, and documents as editable project objects. These patterns do not require execution to reside on the GUI host.

Official documentation is a dated observation: linked pages can change. Public repository references below are pinned to inspected commits wherever available.

## Source versions

| Repository | Inspected main commit | Commit date |
| --- | --- | --- |
| `anthropics/claude-code` | `2301018b1f61073c501a8e7a4813ef48c239163b` | 2026-10-09 |
| `anthropics/skills` | `dbd4588f9e1033efb41dad4bef2f7947c8993d44` | 2026-10-09 |

Selected inspected blob identifiers:

| Asset | Blob SHA |
| --- | --- |
| Claude Code review plugin README | `42cc8095cd2c54e043f9276224df408dca4f48cc` |
| Claude Code review command | `0b27765f17635137853df05bb7c122d39f1af3d7` |
| Skills repository README | `eb54ee8b92f2d40b4b26569be0772be3c1e23156` |
| Document coauthoring skill | `a5a69839ef4a161131d80b6daef10037a9686f4a` |
| Skill creator skill | `65b3a402dbd09b8e83f9d637c6b553875189085c` |
| DOCX skill | `fb954a460a1ea2294e9595e87fecce8df043eeba` |
| PDF skill | `d3e046a5ae107a6cb23cfb16c219837094ab35d3` |

## Skills, MCP, and native services are different layers

- A **skill** supplies task-specific procedures and supporting reference material or scripts. Its instructions guide an agent; they do not by themselves enforce a security boundary or guarantee that a completion criterion was satisfied.
- **MCP** exposes tools, resources, and reusable prompts through a protocol. Claude Code documents resource references, prompt commands, tool discovery, and connection/authentication status. A service may offer MCP without its authoritative state living inside an execution worker.
- A **native product service** may own documents, permissions, revisions, comments, or project state. Claude Docs is publicly documented as a product capability; this investigation did not establish that its underlying implementation is an MCP server or skill.
- A **harness mechanism** schedules agents, applies permissions, evaluates hooks, and controls completion. Public documentation describes many such behaviors without publishing the complete runtime implementation.

The new workbench can combine these layers: durable project services with optional MCP adapters; portable scientific skills; and enforced execution/review controls in the harness.

## Verified capability inventory

| Capability | Observed behavior | Useful project lesson |
| --- | --- | --- |
| Living documents | Claude Docs supports saved rich-text documents, direct user edits, simultaneous editing, anchored comments, requests addressed to `@Claude`, authorship attribution, and actions under the requesting person's permissions. | Treat research documents as durable project objects. Preserve user edits, discussions, revisions, and agent authorship independently of worker lifetime. |
| Discoverable skills | Claude Code loads `SKILL.md` procedures on demand, supports explicit and automatic invocation, and allows supporting files. Some additional frontmatter is Claude Code-specific rather than portable Agent Skills behavior. | Use a portable package format with a small discovery description and detailed procedure loaded when needed. Record the skill version used for a run. |
| Specialized subagents | Subagents have separate contexts, role prompts, scoped tools and permissions, optional preloaded skills, and optional persistent memory. | Define focused scientific roles without equating each agent role to a particular execution container. |
| Independent review | The official public code-review command launches independent reviewers, then separate agents validate candidate issues. Findings cite precise source locations. | Review explicit artifact revisions and preserve the evidence supporting each finding. Adapt the rubric to scientific work. |
| Completion and operation hooks | Hooks can run before tools, at task completion, or when an agent tries to stop; they can return feedback or block some actions. Stop-loop safeguards are documented. | Enforce required evidence and review states in the harness. Bound revision loops and expose inconclusive outcomes. |
| Project instructions and memory | User-written instructions and learned memory have different purposes. Claude Code explicitly treats both as context, not enforced configuration. | Separate project rules, curated knowledge, claims, and temporary agent notes. Permissions and hard constraints belong in executable policy. |
| Shared work coordination | Experimental agent teams share tasks, dependencies, claiming/ownership, status, and messages. | Build a durable research board spanning workbooks, with work items linked to claims, experiments, documents, and reviews. |
| MCP capability discovery | Claude Code distinguishes tools, resources, and prompts, with deferred tool definitions and visible connection/authentication states. | Provide a capability registry with project grants, health, and usable resource references. Keep a model-compatible fallback when deferred discovery is unavailable. |

Official documentation supporting the inventory:

- [Get started with Claude Docs](https://support.claude.com/en/articles/16923645-get-started-with-claude-docs)
- [Extend Claude with skills](https://code.claude.com/docs/en/skills)
- [Create custom subagents](https://code.claude.com/docs/en/sub-agents)
- [Hooks reference](https://code.claude.com/docs/en/hooks)
- [How Claude remembers your project](https://code.claude.com/docs/en/memory)
- [Orchestrate teams of Claude Code sessions](https://code.claude.com/docs/en/agent-teams)
- [Connect Claude Code to tools via MCP](https://code.claude.com/docs/en/mcp)

Agent teams are documented as experimental and disabled by default. Their presence is evidence for a coordination pattern, not a recommendation to depend on that exact runtime feature.

## Living documents and collaboration

Claude Docs directly addresses the user's observation about an editable living document. Its documented behavior includes persistent account-saved documents, document tabs, direct edits by people, real-time collaboration, text-anchored comment threads, and requests for Claude to revise text through a comment. Changes are attributed to their authors, and Claude operates with the permissions of the person making the request.

This does not establish its backend protocol. The correct feature requirement is collaborative document behavior rather than an assumed proprietary MCP implementation.

For this workbench, a proposed document service should support:

1. Stable document and revision identifiers across sessions and worker containers.
2. Direct user edits plus agent-proposed or agent-applied changes under project policy.
3. Anchored comments and replies that remain meaningful as text changes.
4. Authorship and a visible revision history.
5. References to claims, experiments, source pages, equations, and review findings.
6. Conflict handling when a person and an agent change the same passage.

The exact storage and concurrency implementation remains an engineering decision. These are proposed requirements derived from documented behavior, not verified details of Claude Docs internals.

## Review patterns from public assets

The inspected official review command is a usable public prompt asset, but it is a code-review workflow rather than a scientific validation system.

It gathers scoped project instructions and a change summary, launches independent reviewers, and separately validates candidate issues. Its default output is local; publishing review comments requires the explicit `--comment` option in that command. Exact source links make findings inspectable.

There is documentation drift within the inspected repository: the review README describes numeric confidence filtering at 80 and historical analysis, while the actual command uses explicit issue-validation stages and two bug-review perspectives. The command is the better source for its current procedure. Neither a score nor agreement between language models establishes mathematical correctness.

Sources:

- [Pinned review command](https://github.com/anthropics/claude-code/blob/2301018b1f61073c501a8e7a4813ef48c239163b/plugins/code-review/commands/code-review.md)
- [Pinned review README](https://github.com/anthropics/claude-code/blob/2301018b1f61073c501a8e7a4813ef48c239163b/plugins/code-review/README.md)

Scientific adaptation should distinguish:

- Logical validity and assumptions in a mathematical argument.
- Reproducibility of a computation and its recorded environment.
- Correct interpretation of bounded checks or empirical measurements.
- Accuracy and suitability of citations.
- Unresolved findings, accepted findings, and findings invalidated by new evidence.

A review should name the exact document/code/data revision it evaluated. Changes to important assumptions or results should make dependent reviews stale. The harness should limit repeat-review loops and report uncertainty instead of silently declaring success.

## Other public skill patterns worth studying

### Document coauthoring

The public coauthoring skill organizes work into context gathering, iterative refinement, and reader testing. Reader testing uses a fresh context to identify missing assumptions, ambiguity, and contradictions that the authors' conversational context may hide.

This is a useful pattern for mathematical exposition: a reviewer should be able to understand the definitions and argument from the document and cited evidence rather than relying on the author's full session history.

[Pinned coauthoring skill](https://github.com/anthropics/skills/blob/dbd4588f9e1033efb41dad4bef2f7947c8993d44/skills/doc-coauthoring/SKILL.md)

### Skill evaluation

The public skill-creator asset compares skill-assisted work with a baseline, records outcome/time/token measures, collects human feedback, and tests triggering against both intended requests and near misses. It also describes independent or blind comparison where supported.

Use this pattern to evaluate scientific procedures on representative cryptography tasks. Merely having an elaborate skill is not evidence that it improves results. Evaluate correctness, reproducibility, appropriate tool selection, useful review findings, and unnecessary work.

[Pinned skill creator](https://github.com/anthropics/skills/blob/dbd4588f9e1033efb41dad4bef2f7947c8993d44/skills/skill-creator/SKILL.md)

### DOCX and PDF procedures

The public DOCX skill demonstrates comments, tracked changes, structural validation, and rendering the result for inspection. The PDF skill covers text/table extraction, OCR, page operations, and related document utilities.

These are production reference assets according to the repository README, but the README also warns that public examples and actual product behavior can differ. It distinguishes source-available document skills from open-source examples. Learning from the patterns does not imply unrestricted permission to redistribute their implementations.

The inspected skills do not establish first-class mathematical PDF navigation or SageMath integration. Those need explicit product and tool contracts here.

- [Pinned repository README](https://github.com/anthropics/skills/blob/dbd4588f9e1033efb41dad4bef2f7947c8993d44/README.md)
- [Pinned DOCX skill](https://github.com/anthropics/skills/blob/dbd4588f9e1033efb41dad4bef2f7947c8993d44/skills/docx/SKILL.md)
- [Pinned PDF skill](https://github.com/anthropics/skills/blob/dbd4588f9e1033efb41dad4bef2f7947c8993d44/skills/pdf/SKILL.md)

## Research sources and project knowledge

Anthropic's research-system engineering article describes a separate citation stage after research and synthesis. Its evaluation rubric includes factual accuracy, citation accuracy, completeness, source quality, and tool efficiency. It also emphasizes that human evaluation catches problems missed by automated evaluation.

The practical lesson is to check claim-to-source correspondence as a distinct review responsibility. A bibliography alone is insufficient: a result should be traceable to the passage, theorem, page, or computation that supports it.

[How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)

Claude project documentation distinguishes project knowledge from individual chats and supports project instructions and sharing. Its current wording also discusses project memory. These concepts should not be treated as a guarantee that every session's full contents are automatically available to every other session.

[How can I create and manage projects?](https://support.claude.com/en/articles/9519177-how-can-i-create-and-manage-projects)

For this project, use explicit promotion and retrieval of useful knowledge instead of assuming shared data storage supplies shared scientific understanding.

## Proposed research board and ledger

This is a proposed scientific extension, not a verified built-in Claude feature.

Typed project entries could include **question, conjecture, assumption, theorem, experiment, counterexample, decision, and review finding**. Each entry should reference its originating workbook, relevant document/data/code revisions, supporting evidence, dependencies, status, and authorship.

Evidence status should distinguish:

- Mathematical proof or a proof checked by an identified formal system.
- Bounded exhaustive verification over a specified domain.
- Empirical observation with parameters, seeds, and measurements.
- An argument or hypothesis awaiting verification.
- An agent or human assessment that is not itself proof.

The work board tracks what should happen next; the ledger records what was claimed, tested, learned, or decided. They can share a UI and references while keeping these purposes explicit.

Both should be authoritative project services outside disposable worker state. A session with a separate container should still see the project board, published findings, shared data references, and permitted documents.

## First-class mathematics, SageMath, and PDFs

The user requires these capabilities; this investigation did not find evidence that general document skills alone provide them.

Recommended requirements:

1. Preserve editable LaTeX source for inline/display equations and matrices throughout chat, living documents, ledger entries, reviews, and exported scientific output.
2. Keep source representations alongside rendered views rather than storing equations only as images.
3. Link mathematical claims to exact PDF pages or regions, with parsed text and page imagery available for checking extraction errors.
4. Link claims and document passages to Sage inputs, outputs, environment versions, and execution provenance.
5. Record computational checks with their domain and limits so finite testing is not presented as general proof.
6. Mark dependent results and reviews for reconsideration when an assumption, equation, or supporting computation changes.

Detailed math rendering, source anchoring, parser selection, and Sage execution APIs remain engineering decisions for the current plan.

## Local and remote integration lessons

Claude Desktop documents MCP extensions for local tools and separate remote connectors. The underlying product distribution choices differ from this workbench's placement requirements, but the useful pattern is exposing capabilities with explicit configuration, sensitive-setting handling, availability, and connection state.

- [Getting started with local MCP servers on Claude Desktop](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop)
- [When to use desktop and web connectors](https://support.claude.com/en/articles/11725091-when-to-use-desktop-and-web-connectors)

For independent GUI/orchestrator/worker placement, expose stable project-scoped service references and avoid embedding host-specific paths into agent instructions. Some integrations can run as controlled worker-side adapters; others belong behind a trusted service endpoint. Credentials and grants should follow the required capability rather than being copied wholesale into research containers.

Deferred tool discovery is useful for context efficiency, but Claude Code documents model/provider compatibility differences. A new harness should support an explicit discovery strategy and fallback rather than assuming one vendor's tool-reference format works through every model gateway.

## Public system prompts: what is and is not available

Anthropic publishes an official archive of core system prompts for its web and mobile products. The archive explicitly says these updates do not apply to the Claude API. It does not establish a complete Claude Code/Desktop harness prompt or disclose the Docs backend implementation.

Public plugin-command prompts, skills, and example subagent prompts provide concrete evidence about selected workflows. They can inform a new design without requiring wholesale reproduction of product prompts.

No authoritative complete Desktop runtime prompt was found in the examined sources. Third-party leaked prompt collections were excluded. Product documentation, public prompt assets, and inferred recommendations are kept distinct in this report.

[Official system-prompt archive](https://platform.claude.com/docs/en/release-notes/system-prompts/overview)

## Recommended incorporation order

This is an investigation recommendation, subject to the current planning documents:

1. Preserve existing scientific skills and automatic reviews while establishing the orchestrator-to-worker execution boundary.
2. Add durable project board/ledger services and stable cross-workbook references.
3. Make LaTeX, SageMath, and navigable PDF evidence part of the core research contract.
4. Add living documents with user edits, anchored comments, revisions, and review invalidation.
5. Evaluate scientific skills and reviewers against representative tasks, baselines, and human judgments.

The investigation verified source behavior and design patterns. It did not validate an implementation, deployment, sandbox policy, or scientific workflow in this new project.
