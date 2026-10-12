# Document comments browser acceptance on babbage

Run on 2026-10-12 against signed implementation `5c74a0d`, integrated as
`4ca094d` over main `4268632`. The production workspace build ran in a
transient user unit on loopback port 35995, with disposable synthetic project,
document and annotation records. No OpenShell sandbox or real model was used.

The [fixture helper](../../labs/document_comments.py) seeds two neutral formula
comments, runs a scripted document-only main agent, appends concurrent history,
and exports [token-free records](document-comments-evidence-2026-10-12.json).
The initial create/respond steps used the same helper before formatting and
adding its record mode. Its WorkerClient has no tool that makes an HTTP call;
the recorded provider and worker call counts are both zero.

## Observed browser behavior

| Interaction | Result |
| --- | --- |
| Comment draft | Selected stable formula m2 in block b3, entered a request and reloaded. Its text, revision 1 and formula anchor survived |
| Addressed request | Addressed the comment to session a and posted it. The GUI displayed the queued request with USER attribution |
| Main-agent response | A separate scripted harness run read the document, patched the formula and replied to the thread. The fsynced run retained the request ID; refresh displayed responded status and the exact run/model attribution |
| Anchor projection | The changed formula became stale, retaining its revision-1 quote beside the bounded current excerpt. The untouched equation's comment stayed current |
| Change report | The agent reply linked revision 2, named b3 and reported no changes outside the anchor. The summary remained unchanged |
| Resolution history | Resolve and Reopen appended USER entries 3 and 4; the response and work-request history remained visible |
| Concurrent history | Kept an unsaved reply while another process appended 55 entries. Refresh showed the latest 50, retained the draft and its base revision 1, and offered explicit adoption of current revision 56 |
| Pagination and reply | Loading earlier entries restored all 56 entries without losing the draft. Explicitly adopting revision 56 and posting appended USER entry 57 and cleared the submitted draft |

Screenshots show the [queued request](document-comments-queued-2026-10-12.jpg),
[responded thread and resolution history](document-comments-response-2026-10-12.jpg),
and [retained stale draft](document-comments-draft-2026-10-12.jpg). The browser
reported no warning or error logs. The synthetic formula replacement exercises
revision/anchor plumbing; it does not validate mathematical judgment.

## Review and checks

Independent adversarial review cleared exact implementation `5c74a0d`, including
late final-turn delivery, full request-body preservation, the 128-request run
cap, paged history bounds and malformed cursors. Automated tests cover checked
agent scope, authority, stale revisions and same-origin USER writes; the browser
run did not simulate a dropped network response or a second browser session.

The integrated branch passed 9,040 Python tests (43 opt-in tests skipped) and
418 web tests. Ruff, strict Python/TypeScript checks, license checks, generated
contracts and the production web build passed.

## Limits and cleanup

An idle session queues a request for its next manually started main run.
Automatic idle-session wake-up and the live cross-turn scheduler remain open.
Agent comments cannot wake another agent. This slice implements document
block/formula/range anchors; artifact/PDF anchors, annotation export and the
project ledger are separate work.

The browser tab was closed, the transient server stopped, its marked synthetic
state directory removed, and port 35995 checked free. No persistent configuration
or real project data changed.
