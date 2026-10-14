# Pinned artifact review browser acceptance — 2026-10-12

The production workspace GUI and authenticated HTTP API were checked on babbage
against new, marked synthetic control-store state. The frozen, independently
reviewed source was `9995f8a4f7513e1abbd5495d7ad9f2b5c66a6eac`, combining the
artifact consumer with merged document math PR #83. Python was 3.13.15. Exact
production asset hashes and observed responses are retained in the
evidence archive (archived outside git, sha256 `4cdf92682f6c`).

The reusable
[`artifact_review_browser_fixture.py`](../../../packages/orchestrator/tests/fixtures/artifact_review_browser_fixture.py)
seeds an original count of 16, a corrected count of 15, an empty file, binary and
non-UTF-8 files, and separate session/project scopes. Confirmation uses
`ScriptedModel`; no live model, worker, research execution or OpenShell VM was
contacted. This validates plumbing and authority, not model judgment.

| Check | Observed result |
| --- | --- |
| Immutable original | Revision 2 already existed when the original finding read revision 1. Browser `textContent` and the HTTP response exactly preserved all 35 bytes, including CRLF, quotes and a backslash. |
| Corrected source | The confirmation preview exactly preserved revision 2's 35 bytes and its separate SHA-256. No mutable path was read. |
| Empty text | The preview displayed “Empty source (0 characters).” outside an empty source element. The successful HTTP read retained `empty_source`; the fixture asserts its audited read. |
| Incomplete content | Binary and non-UTF-8 items remained incomplete. Reads showed bounded refusal metadata, with no invented source text or successful coverage claim. |
| Delayed selection | A two-second delayed original read did not overwrite the subsequently selected empty source. |
| Session change | Navigating to the other session during a delayed read left no original finding or preview there. |
| Scope and integrity | Authenticated cross-session and cross-project evidence reads returned 404. A forged replacement hash returned 409 and did not create a claim. |
| Exact fix and replay | The authenticated USER claim named the canonical corrected revision. Replaying the same operation succeeded without a second transition. |
| Confirmation | The fixture checked the exact original and changed evidence, asserted their read-audit entries, and finished `passed` with the finding `confirmed fixed` at revision 3. The browser showed reviewer → USER claim → reviewer confirmation attribution. |
| Browser diagnostics | No warning or error console entries. |

Screenshots retain the original pinned source (archived outside git, sha256 `2e4f5388892e`),
empty source and incomplete inventory (archived outside git, sha256 `f74898efae60`), and
confirmed fix with history (archived outside git, sha256 `687d9ab42209`). All three
were visually inspected. The evidence JSON includes browser observations, HTTP
responses, canonical confirmation state and its audited evidence IDs. An optional
export of all SQL audit tables failed before teardown and is not included.

## Validation boundary

The combined source passed 9,494 Python tests (43 opt-in skips), 436 web tests,
locked installs, static checks, licenses, protocol generation checks and both
production builds. Independent probes also covered durable cursor races, bounded
corrupt metadata and replay of pre-upgrade finding responses. Fresh PR CI remains
the merge gate.

This slice consumes explicit canonical artifact publications. Automatic shell
output inventory, rich preview/download, imports, retention and producer/input
lineage remain open. Typed artifact replacement claims are available through the
authenticated API and main-agent tool; the GUI still has its existing document
revision input and has no artifact-reference editor. Document math behavior
remains the separately validated document-only D82 slice.

## Cleanup

The created browser tab was closed, transient loopback server stopped, marked
synthetic state directory removed, and port 35998 verified free. No persistent
service or provider configuration was created. Only token-free records and
screenshots are retained.
