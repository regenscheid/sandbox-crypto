# Pane resizing and ledger navigation — 2026-10-17

Scope: targeted changes to the existing Local Codex interactive prototypes.
Production application code and APIs are unchanged. The owner requested
continuous right-pane dragging and a clear route back from ledger to session.
The [workspace interaction specification](../../../docs/specs/workspace-browsers.md)
now records those requirements.

## Result

- Shared desktop separator for session and list/detail panes, capped at half the
  viewport. Pointer capture, cancellation cleanup, keyboard arrows/Home/End,
  accessible size values and a local width preference. Resizing changes columns
  without rebuilding content. Smaller layouts retain their existing drawers.
- Ledger keeps the project/session sidebar and session search. Kind filters stay
  above the list. Back to session uses a known session route; existing workspace
  persistence retains unsent drafts and open documents. A same-tab ledger view
  snapshot preserves filters, selection and detail tab across the round trip.
- Independent source review found a regression in returning from Issue list or
  Frontier. The ledger sidebar now invokes its home action instead of reloading
  the saved destination. The reviewer rechecked the fix with no remaining findings.

## Validation and limits

Host browser checks at 1280 × 720 verified pointer sizing to 640 px and back to
520 px, keyboard Home to 320 px, a 10 px arrow step and End to 640 px. The chosen
width survived navigation; Skills also rendered at 640 px. Closing the session
pane removed the separator and restored all 1,070 px of content width to chat.
The notebook and an unsent synthetic chat draft survived a ledger round trip;
the test draft was then cleared. Claims filtering and Evidence-tab selection
survived returning to ledger. List/board, session search, and return to the full
ledger from both Issue list and Frontier were checked. No body overflow was
observed at the document pane maximum.

All three JavaScript sources pass syntax checks. Both artifact bundles include
the shared CSS/JS siblings without truncation. No dependencies were installed,
no real research or connector calls were started, and no production build was
needed. Mobile rendering and native input remain unverified in a browser.

## Evidence

Bundle `workspace-design-20261017`, outside git. These are current artifact
hashes; the previous report retains its earlier snapshot hashes. Both HTML
files require their sibling `pane-resize.js` and `pane-resize.css`.

| File | SHA-256 |
| --- | --- |
| `workspace-redesign.html` | `676b7ab5761688dee84cd37096792cbe343965435620e65977f88b3d83859586` |
| `ledger-redesign.html` | `9a2b74d283a6d7d3bcbcec44b3ec64032f76f31822e8a3900b9caf42a7b43860` |
| `pane-resize.js` | `b89e52bb08e5e5fd481cd1283f6c88afec7b274e716fb8ff7a722e18d14bc756` |
| `pane-resize.css` | `bdbb67571b49307268191fd5752379510fd50eecce170232027d0313efa1f5d1` |
| `workspace-design-notes.md` | `050191d0b29192b5e64e81bf68075f6553500d6a805890322e2198de113c061e` |
| `ledger-design-notes.md` | `d4f35280956f8cc62ba26d208522b758ec4095c01355dd92dfc1d755c1a65a02` |
| `ledger-navigation-resize.png` | `8852d1752c432c2ec52982c0d83d90608e2cbd104cb7f7a005a704332319d437` |
| `session-draggable-pane.png` | `fb3db12e15040fd05378be5b8d2ec76a7a62a547bb753950415e6ab71219f8de` |
| `resize-navigation-checks.json` | `6754b64927d27f726c977998b349458f4282a05f683b01ffa3061637efab9322` |
