# Project math macros (LD-28)

Project macros are a versioned USER setting in the shared control store. A
document's YAML front matter overrides project definitions of the same name.
Comments use their parent document's effective context; other project text uses
project definitions. This implements the setting, rendering and existing
mechanical-check paths. Export staging, the ledger and additional D82 prose
sources remain separate work.

## Changing the setting

The project view has a **Project math macros** JSON editor. Its draft and retry
operation ID survive reload and an uncertain response. A concurrent setting
change returns a conflict; **Use current macro revision** changes the draft's
base without replacing its definitions. Settings refresh keeps child views
mounted so existing editor drafts survive. Other open document/review views
adopt a newly loaded context when refreshed or reopened.

`GET /api/v0/projects/{project}/macros` reads the current revision.
`POST` to the same path accepts exactly:

```json
{"expected":0,"macros":{"\\F":"\\mathbb{F}"},"operation_id":"set-project-macros"}
```

The workspace's existing authentication and same-origin write guard apply.
Project existence and USER attribution come from the server. Agents, workers,
imports and platform actors cannot change this setting. A replay returns the
exact setting revision that operation produced, even if another operation has
since changed the head. A changed payload under the same operation ID refuses.

The equivalent explicit USER CLI action reads definitions from a bounded file:

```sh
sandbox-science-harness project macros --state-dir STATE --project PROJECT \
  --macros-file macros.json --expected 0 --operation-id set-project-macros
```

Omit the last three options to read the current setting. Invalid file/JSON
errors are classified without echoing the file's contents.

## Bounds and resolution

A setting admits at most 200 definitions, 1,000 characters per definition and
16 KiB of canonical UTF-8 JSON. Definitions must be control-sequence names and
must satisfy the existing front-matter expansion limits: no nested definitions,
duplicated arguments, recursion or expansion beyond 2,000 characters. The
effective document/project combination is checked again; document definitions
win, with explicit warnings for excluded project definitions or unsafe combined
expansion. Ordinary document edits retain the existing front-matter warning
behavior rather than becoming settings writes.

Each project retains at most 1,024 changed setting revisions and 4,096 operation
receipts. No automatic setting history eviction occurs. Macro tables are
additive; read-only legacy views do not create schema or mutate check cursors.

## Exact contexts and uncertainty

Each new document revision records its project setting revision atomically with
the source event. A current unnumbered document view uses the current project
setting; an explicitly numbered historical view uses the saved setting pin.
A legacy source without a save-time pin reports unknown historical context,
rather than reconstructing an earlier setting from today's head.

Each admitted mechanical attempt pins the project setting revision/digest and
retains its resolved effective definitions, warnings and context identity.
Comment attempts retain their existing exact body and parent document macro
revision pins. Thread history is explicitly displayed under the selected
document's diagnostic context, not claimed as its original render; exact checked
context remains in check evidence. MAIN fix/rebut explanation sources pin their original finding's
document anchors at source creation, keeping those anchors through subsequent
body corrections. Their current check combines those immutable anchors with
the attempt's pinned project setting. Body history also retains its save-time
project revision. Sources with no document anchors use the project setting.

When several finding document anchors resolve to the same effective definitions,
that agreed context is used. Conflicting, missing or unproved contexts hold
mechanical checks as unavailable and produce no authoritative math finding or
removal decision. The GUI may show project-only diagnostic rendering, accompanied
by an explicit warning. Reviewer snapshots include the explanation's diagnostic
context and are incomplete when that context is held; scientific decisions and
explicit USER authority remain independent of mechanical rendering.

Reviewer explanation confirmations fence both exact body revision/digest and
project setting revision/digest. A setting change during review makes that
confirmation stale. Confirmation eligibility and retry identity include the
new setting revision, so an ended old-context review does not prevent a fresh
confirmation. No new reviewer tool category or source editing privilege is
introduced.

Render and repair cache keys carry project and effective context identity.
Project-scoped repair requests also isolate the server repair cache; their
bounded identity is a cache key, never document read authority. Legacy unscoped
repair requests remain compatible. The renderer remains operator-configured,
with mechanical rendering unavailable by default; this setting starts no model
request, service, worker or idle scheduler.

## Refresh and mechanical lifecycle

A settings write only appends one bounded setting and receipt. The existing
active math controller discovers stale current-head contexts lazily, admitting
at most 32 sources of each kind per refresh step. Its normal attempt/history,
batch, lease and actual-work cancellation limits still apply. Immutable effective
context storage has a global 64 MiB logical budget; exhaustion holds checking
and remains visible without vetoing an ordinary source edit or setting change.

A late result under an old setting remains immutable evidence but cannot change
current findings, prove removal, supply a new closing notice or finalize staged
unaddressed state. A fresh context attempt checks all spans. A dependency-only
change refreshes exact assessment/evidence without spending the two failed
author-block-edit reflags. USER decisions, source ownership, shared two closing
response rounds and save-before-ACK remain unchanged.

## Verification boundary

Focused fixtures cover USER authority and scoped HTTP guards, CAS/idempotent
history, rollback, definition/combined expansion bounds, project isolation,
document overrides, comment and explanation inputs, historical unknown state,
lazy refresh, stale renderer/confirmation/closing ACK races, dependency-only
budgets, ambiguous anchors, context quotas and corrupt retained-state reads.
Frontend fixtures cover cache/request identity and visible ambiguity warnings.
Native browser acceptance and final combined gates remain required before
delivery; no live provider or service is exercised by these fixtures.

The preliminary source milestone passed 219 affected Python cases and 128
focused frontend cases, with Ruff, formatting, Pyright and Svelte/TypeScript
checks clear. These are affected-path results, not the final combined full-suite
or native-browser gate.
