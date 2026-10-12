# Document editing browser acceptance on babbage

Run on 2026-10-12 against signed implementation `13bc226` integrated as
`e998cb1`, with the provisioning cleanup follow-up `597519c` already present.
The production web build ran in a transient user unit on loopback port 8101,
with a fresh disposable ProjectStore and SQLite control store. No worker,
OpenShell sandbox or model gateway was needed for these control-store flows.

[Recorded revisions and events](document-editing-evidence-2026-10-12.json)
contain only synthetic research notes and identities. The reusable
[fixture helper](../../labs/document_editing.py) creates that data, simulates
concurrent agent changes, and exports the final store without browser tokens.

## Browser checks

| Interaction | Observed result |
| --- | --- |
| Source draft retention | Edited the conclusion from revision 2, reloaded the tab, and found the unsaved text intact. Saving appended revision 3 attributed to the user and preserved the other blocks |
| Concurrent block edit | Kept a user draft for b2 while a separate process appended agent revision 4. Save returned a conflict, retained the draft, and displayed the current block and its author |
| Block resolution | Explicitly selected the current revision as the base, then saved the retained block draft as user revision 5 |
| Source conflict resolution | A second concurrent agent edit produced revision 6. Source save conflicted; starting resolution loaded revision 6 and kept the old source in a separate read-only draft. Deliberately copied the wanted change and saved user revision 7 |
| Unsafe retained patch | The original suggestion named b2, removed by the source edit's conservative identity reconciliation. Preview reported the missing block and disabled Apply |
| Historical restoration | Restored revision 2 as new user revision 8 in the same document, retaining the original block IDs and all intervening history |
| Reviewed suggestion | Preview against revision 8 displayed current and proposed text. Explicit Apply appended revision 9 attributed to the original agent, with acceptance attributed to the user |
| Workbook promotion | Moved the workbook into the project. Its document ID, revision 1, text and original agent attribution remained intact |

The [conflict view](document-editing-conflict-2026-10-12.jpg) shows the retained
user draft alongside the current agent block. The
[accepted suggestion view](document-editing-accepted-patch-2026-10-12.jpg)
shows the accepted-by-user attribution and rendered equation. The browser
reported no warning or error logs during these flows.

Independent adversarial review cleared implementation commit `13bc226` after
fixing uncertain-response retries: the browser persists operation IDs for the
same scoped payload until a definitive parsed response arrives. Review also
checked stale reconciliation, exact-head patch acceptance, preservation of
unnamed blocks, reverts, ownership, promotion name collisions and same-origin
USER attribution. These API edge cases have automated coverage; the browser
run above did not simulate network packet loss or a second browser session.

## Integrated checks

The final integrated branch, including the background-job cleanup and bounded
lockfile serialization follow-ups, passed 8,999 Python tests (43 opt-in tests
skipped) and 414 web tests. Strict Python and TypeScript checks, Ruff, license
checks, generated contracts and the production web build also passed.

## Limits and cleanup

The editor is Markdown source or one block at a time. Source conflicts start
from the current text and require deliberate reconciliation. Ambiguous source
changes can remove and add block IDs rather than guessing continuity. Rich-text
editing, GUI suggestion decline, deleted-document restoration, comments and
work requests are separate slices.

The acceptance tab was closed, the transient server stopped, its disposable
state removed, and port 8101 checked free. No persistent configuration changed.
