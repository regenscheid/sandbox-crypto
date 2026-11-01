# Architecture audit before the cluster stage (2026-10-23)

Read-only audit of `k8s-sync` at `aca5b8d3`, by four independent readers (containment,
complexity, research friction, code health), synthesized for the owner. Nothing was
changed. The owner's frame: Cryptography Atlas is a Claude-Science-like assistant for
cryptography research; agents install and compute freely inside sandboxes, reach the
world only through MCP services and package repositories, and must not take unwanted
actions. Confidentiality of workflow data is out of scope; the owner's systems, third
parties and research-record integrity are in scope. Reads are free and recorded;
effects are gated. Friction is the main project risk. The cluster is the next stage,
and the owner wants the architecture settled before more implementation there.

## Summary

- **The containment boundaries are sound.** No path was found by which an agent can
  change policy, grant approvals, reach another project, publish packages, push to a
  repository or write on the host. The worker, control and MCP proxy surfaces are
  scoped and authenticated.
- **The gates key on the wrong things.** Tools are classified by name, not by what
  their arguments do, so Firecrawl's `scrape`/`crawl` with browser `actions` and
  `webhook` are allowed while `interact` is held. A remembered "server" approval
  covers every caller, including research code, and every tool the server adds later.
- **Routine reads are still gated, and the approval path for them is unusable.**
  Git clones from unlisted hosts and GETs on odd ports escalate and then time out, and
  network/package approvals can only be answered from a command line on the host.
  The SNOVA run shows the cost: 7 of 8 resumes blocked, each needing the operator.
- **About 18k of 167k source lines are failure-recovery machinery, and roughly 5k
  (plus 6k test lines) exist only for a cluster that has never run.** MCP is enforced
  at three layers. The docs are about 620k words across `docs/` and `.agents/`.
- **Operations are thin.** Nightly CI has been red four of the last five nights, there
  is no backup or restore runbook, migrations are forward-only without a snapshot step,
  no telemetry pipeline exists, and the Kubernetes path has no automated coverage.

## Findings by lens

### A. Containment (unwanted actions)

| # | Finding | Evidence | Severity |
| --- | --- | --- | --- |
| H1 | `firecrawl_scrape`, `firecrawl_crawl` and `firecrawl_agent` are allowed by name although their arguments carry effects: `actions` (click, write, press, executeJavascript), `profile.saveChanges`, `webhook`/`webhookHeaders`. The project's own fixture classes them WRITE; `test_example_policy.py:41-52` allows them "on cost grounds". Research code inherits the same tools (`examples/policy.yaml:115-116`). | `policy/evaluate.py:623-693` decides on tool name alone | High |
| H2 | A remembered approval (`Grant`) auto-approves every caller kind, every session and future tools: `PendingApproval` carries no caller kind or run; a "server" grant covers all escalated tools of crypto-research-mcp, a portal for ~8 backends, and any tool a later revision adds. | `workspace/approvals.py:266-300`, `approval_settings.py:38` | High |
| M1 | HTTP approvals are keyed on method, host and path; body and query are never recorded or shown. One approved POST allows unlimited posts with any body for 8 h (up to 720). | `approvals/keys.py:13-23` | Medium |
| M2 | GETs to any public host are free with no rate limit; the "proxy rate limits" DA-5 mentions do not exist. Consistent with "reads are free", but the one ungated way to harm third parties (volume, GET-triggered effects). | `examples/policy.yaml:67`, `worker/settings.py:112` | Medium |
| M3 | On Kubernetes, research egress denial depends on OpenShell's chart and the CNI; the bundle renders NetworkPolicies only for app, pool and manager Pods. Unverified. | `launcher/kubernetes_bundle.py:100-186`, `docs/KUBERNETES.md:52` | Medium |
| L1 | Worker policy revisions can be granted but never revoked. | `worker/app.py:550-565` | Low |
| L2 | `resources/read`, `prompts/get`, `completion/complete` pass without tool rules. | `evaluate.py:44-57` | Low |

Nothing found: policy change, approval grant, cross-project reach, package publishing,
host writes, ungated push or HTTP write. Compromised-server protections already in
place: no client capabilities (no sampling/elicitation/roots), notifications dropped,
binary results replaced, tool descriptions sanitized, schemas rebuilt from structure,
unknown tools refused, redirects and cookies refused. Not blocked: injection through
result text (accepted), and a server silently changing what an allowed tool does.

Dead weight under the new threat model (exfiltration guards): `gitproto.py` (759
lines), `max_url_bytes`, the egress-volume monitor, query-size logging.

### B. Complexity

- 166,803 Python source lines, 169,249 test lines, 68.6k TypeScript/Svelte. Orchestrator
  94k (context 10.7k, ledger 10.0k, documents 9.5k, consistency 9.0k, continuity 5.7k).
- Protocols: worker v11, control v3 (33 operations), executor v3, middleware 1.0,
  capture v1; at least 12 versioned file formats; 77 control migrations (103 tables),
  153 tables in all; 94 decisions, 42 specs; 2,306 commits since 2026-10-10.
- Runtime on babbage: about 9 processes plus 3 or more VMs. Kubernetes: 3 Pods plus
  two Pods per sandbox.
- Recovery machinery (~18k lines): needed on a homelab: execution journal/replay,
  executor daemon and incarnation fence, recovery fence. Deferrable: credential
  generations (k8s), continuity automation (~5.7k; manual Resume would do), artifact
  retention outbox/replica release/S3 (~1.1k), workspace checkpoints (1.2k), prefix
  continuity, event-store hash chain (agents cannot reach that database).
- Duplication: MCP enforced at OpenShell native rules, the proxy (3.3k lines, its own
  sandbox and host forward) and the middleware; control and workspace as separate
  processes over one database; worker, middleware and cache as three local units.
- Dormant: host `harness serve` path (tests only), k8s features exercised only by
  their own tests, 30 OpenShell 0.1.3 mentions, untested Mac paths.
- Docs: `docs/` 117 files, ~378k words; `.agents/` 225 files, ~246k words; the minimum
  reading path for a contributor is ~50k words.

### C. Research friction

| # | Scenario | Today | Under "reads free, effects gated" |
| --- | --- | --- | --- |
| 1 | Clone from an unlisted git host | escalate, denied after 30 m | allow `upload-pack` anywhere |
| 2 | GET on a non-default port | escalate | allow, record |
| 3 | Read APIs that use POST | escalate | reviewed read-only POST list |
| 4 | Any new host | admission takes 9–20 s and cuts downloads; 64-host cap | pre-admit research hosts; raise cap |
| 5 | Answering a network/package request | **CLI only on the host; GUI shows nothing**; 8 h, per sandbox and revision | GUI card, remembered per project and host |
| 6 | Non-default conda channel | denied | escalate or allow known channels |
| 7 | `provision` tool | wheels only, catalog of 9, no g6k | allow sdists; extend catalog |
| 8 | Install cancelled/OOM/worker restart | `needs_recovery` refuses all work; only deleting the environment clears it | user clear/restore action |
| 9 | Call queued behind an install | wait counts against the call's 120 s | don't count the wait |
| 10 | AGPL package | refused unless excepted | alert for research environments |
| 11 | `firecrawl_interact`, monitors | held; **the whole turn waits up to 30 m** | hold the call, let the agent continue |
| 12 | 30 h job | killed at the 24 h default | default to the 7 d maximum |
| 13 | 48 GiB job | no profile fits (crypto-base 16 GiB; largest job profile 32 GiB) | a ≥64 GiB profile on `worker9` |
| 15 | Detached job finishes | no wake; next-turn notice only | wake within budget |
| 16 | Delegated child computes | `ChildFence` refuses Sage, writes, installs, even PDF reads while the plan is a draft or after a user edit | fence outward effects only |
| 17 | Budgets | run stops; Resume manual; child extensions pause the tree | auto-resume transient failures |
| 18 | Idle suspension | kernels lost; waiting work isn't "use" | off by default, or replay |

Recorded stalls: SNOVA (7 of 8 resumes blocked; $305.76 in ~10.5 h; reviewer capped
on 97 of 107 runs), lab 39 (policy denied every MCP tool), lab 21 (cancelled install
left the environment unusable), lab 10 (admission cut a download). No lab has ever
exercised a network or package approval in real research.

### D. Code health and operations

- 15,447 tests (fast 14,388, slow 1,059); PR CI 12–16 minutes; 11 of the last 26 PR
  runs failed; nightly red 4 of 5 nights (the `repeat` job cannot finish in 90 min;
  two named failures; `full (3.13)` cancelled on 3 of 4 nights).
- 25 tests skipped by default (integration/images/live) leave S3, real pip/conda
  clients, images and live gateways unverified; the Kubernetes path has zero CI.
- OpenShell version hardcoded in 4 places, never verified at launch; no Dependabot,
  `pip-audit` or `npm audit`.
- No backup/restore runbook; migrations forward-only with no pre-upgrade snapshot
  (77 steps in one 3,361-line file); no telemetry (no OTLP, Prometheus or JSON logs);
  secrets as plain files.
- `.agents/`: `BACKLOG.md` 590 lines, 38 of 126 reports unreferenced, `STATUS.md`
  carries three disagreeing test counts. It serves agents, not the owner.

## Recommendations

### Architecture decisions to settle before cluster work

1. **Write the threat model as now decided.** Protected: the owner's systems and
   network, third parties, research-record integrity. Not protected: workflow
   confidentiality. Reads are free and recorded; effects are gated. Retire the
   exfiltration guards (gitproto, `max_url_bytes`, egress-volume monitor) as
   requirements; code removal can follow.
2. **Classify tools by effect, including arguments.** Tool name alone is not enough
   where arguments carry effects (`actions`, `webhook`, `saveChanges`). Hold or deny
   Firecrawl's action/webhook arguments and `firecrawl_agent`, or confirm the portal
   runs Firecrawl in safe mode and pin that. Use MCP `readOnlyHint`/`destructiveHint`
   as the default for new tools (DA-7 rejected them; revisit under the new frame),
   with the policy able to override.
3. **Rework remembered approvals.** Grants carry caller kind (agent vs research code),
   tool and policy revision; no server-wide grants; a grant never covers a tool added
   later. HTTP approval keys include a body digest and show the payload. Because reads
   no longer escalate, grants are only for effects and can stay narrow.
4. **Choose one MCP enforcement layer.** OpenShell 0.1.5 native `protocol: mcp` rules
   (tool allow/deny, no arguments) plus the supervisor middleware (full request body up
   to 4 MiB, response inspection, ≤30 s per decision) cover nearly everything the proxy
   does; the proxy's remaining roles are the tool index and an extra sandbox hop. Decide
   by prototype: route the attack catalog through native rules plus the middleware.
5. **Minimal cluster scope (amend D92).** First deployment: control+workspace, worker
   pool, manager, Kata sandboxes, Garage for artifacts. Park behind a flag or branch:
   retention outbox and replica release, workspace checkpoints, prefix continuity,
   S3 maintenance cursor. Keep credential generations (the manager needs them).
6. **Unblock delegated compute.** The child fence gates outward effects only; PDF
   tools are reads; children get read-class connectors.
7. **Verification before "deployed":** the cluster acceptance must prove that a
   research sandbox's egress is denied by OpenShell/CNI (M3), that Kata applies to the
   workload Pod, and that a research sandbox cannot reach control's runtime port.

### Fix before the first cluster deployment (small, high value)

- Firecrawl classification (H1); grant scoping (H2); body digest in HTTP keys (M1).
- A per-session volume alert on fetch and crawl calls (M2).
- Reads free in the example policy: `upload-pack` to `*`, GET/HEAD on any port,
  `on_timeout: allow` for read rules; pre-admit research hosts; raise the host cap.
- GUI approval cards for network and package requests, remembered per project.
- Recovery fence: a user clear/restore action; the queued wait not counted.
- Job defaults: 7 d limit; a ≥64 GiB profile; session wake on job end.
- Operations: a backup/restore runbook with SQLite `.backup` before migrations; one
  OpenShell version constant checked at launch; fix or quarantine the nightly failures
  and shrink `repeat`; a `kind` smoke job for the renderer.

### Defer or remove

- Continuity automation beyond manual Resume (decision D87) until real sessions show
  what it buys.
- Squash the 77 migrations into a baseline (as the worker does) after the personal
  instance cuts over.
- Delete the host `harness serve` path. Merge middleware and cache into one local unit.
- Docs: archive historical plan pages and the 38 unreferenced reports; fold the 9
  small worker pages into `WORKER.md`; cap `STATUS.md` at one screen for the owner.

## Topics for discussion

1. **MCP layer:** proxy, or native rules plus middleware? The proxy's extra hop is
   defence in depth; the middleware is one fewer process and parser.
2. **How remembered approvals should scope:** per tool and project is safe; per
   server is convenient and dangerous. Is a time limit wanted?
3. **How much automatic recovery** the research tree needs versus a reliable manual
   Resume. SNOVA's stalls were budgets and suspension, not crashes.
4. **Process consolidation:** control+workspace into one service; worker+middleware+
   cache into one unit. Fewer processes, one larger trust boundary each.
5. **Budget model:** daily caps versus observed use (~$300/day on SNOVA); extensions
   without pausing the tree; a separate reviewer budget.
6. **The orchestrator's 94k lines** (ledger, documents, consistency, context) are the
   research product, outside this audit's scope, and the largest mass by far.
7. **Telemetry:** OpenShell OCSF/OTLP plus the proxy and middleware records into a log
   store, before any intrusion detection.
8. **Multi-user:** one instance per trusted group (D94) is decided; per-person logins
   for attribution are not designed.

## Method and limits

Four readers worked from the docs and code of `k8s-sync`, read-only, with file
references checked by the coordinator where they bear on the conclusions. Nothing was
run against a cluster; Kubernetes findings are from rendered manifests and OpenShell
0.1.5 source. Line counts are from `wc`/`cloc`-style tallies and may differ slightly
from other tools.
