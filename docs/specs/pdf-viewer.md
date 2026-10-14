# PDF viewer

**Status.** Approved, 2026-10-14: the coordinator adopted the six recommendations below under the owner's autonomy mandate (refactoring plan, Phase 4 item 5, E-08). It applies D54 and the threat model's T6 to PDFs; the [PDF sources](pdf-sources.md) spec covers how the agent reads them and leaves the viewer to this one.

## Scope

The user reads a published PDF in the workspace: views its pages, moves between them, zooms, selects text and copies an anchor to a place on a page. Annotations stored on a PDF (research artifacts, RA-21), other rich previews (HTML, SVG, notebook output) and files not yet published are not covered.

## Threat model

A PDF is untrusted content ([threat model](../THREAT-MODEL.md), T6, zone Z4): it can carry script, forms, links and malformed structures aimed at the renderer. The viewer MUST keep it from acting in the user's workspace session. The controls are those T6 names: a separate origin, a sandboxed frame, a strict CSP and PDF.js with scripting off.

## Decisions

- PV-1. **A second origin on a second port.** `sandbox-science-workspace serve` listens on a second port, `--preview-listen` (by default the main port plus one, on the same host), loopback only unless `--allow-remote` as for the main port. That listener serves only the viewer's bundle and PDF bytes for a valid capability (PV-3). It has no API, no login and no session, and it MUST NOT read cookies: a browser sends the workspace's cookie to every port on the host, so a request with that cookie gets exactly what one without it gets.
- PV-2. **The frame.** The workspace embeds the viewer in `<iframe sandbox="allow-scripts allow-same-origin">`. The frame keeps the preview origin, which is not the workspace's, so it cannot read the workspace's pages or call its API, and the workspace refuses a POST from any other origin. The preview origin's CSP is `default-src 'none'`; scripts, styles, the worker, fonts and connections from the preview origin only (`connect-src` names only the preview origin, so a compromised frame cannot even send a request to the workspace's port); images also from `blob:` and `data:`; `frame-ancestors` the workspace's origin; `base-uri` and `form-action` `'none'`. The workspace's own CSP gains `frame-src` for the preview origin.
- PV-3. **Capability URLs.** The workspace hands the viewer a document as a short-lived capability: an HMAC-SHA-256, under a key made at random when the process starts and kept only in memory, over the project, the artifact, the revision, its SHA-256 and an expiry 10 minutes ahead. The preview origin accepts one spelling of a capability (unpadded base64url), compares MACs in constant time, refuses a forged or expired capability, reads the bytes through `ArtifactInventory.content`, which checks their size and SHA-256, off the event loop and a few at a time, refuses anything whose media type is not `application/pdf`, and caps a PDF at 64 MiB. It answers with the bytes as an `attachment` under a CSP that adds `sandbox`, since only PDF.js fetches them. The workspace's own listener never serves the viewer's bundle.
- PV-4. **Published PDFs only.** The viewer opens published artifact revisions ([artifact inventory](../ARTIFACTS.md)); a file in a session workspace is published first. The workspace gets one read-only route that lists a project's published artifacts, and the project page lists them, with a link to view each PDF.
- PV-5. **The renderer.** PDF.js (`pdfjs-dist`, Apache-2.0, tier 1), through its core API only: pages on a canvas with a text layer for selection, no viewer application, no forms, PDF scripting off and `isEvalSupported` false. The version is pinned to a release at least two weeks old, and its character maps and standard fonts are checked against the [license manifest](../LICENSING.md); one outside tier 1 stops the work.
- PV-6. **What the user can do.** View pages; move by page number and by printed page label; zoom; select text; and copy an anchor. The anchor is a `PdfAnchor` ([PDF sources](pdf-sources.md#anchors)): the source's SHA-256, the page's index and label, a box in PDF points and the quote. The frame sends it to the workspace with `postMessage`, and each side checks the other's origin; the workspace shows it and copies it, for pasting into a comment or a chat message. Annotations stored on a PDF stay in the backlog (RA-21).

## Acceptance fixtures

- **PV-F1 Capabilities.** A capability the workspace minted opens its PDF; a forged one, an expired one, one naming another revision's hash, and one for an artifact that is not a PDF are refused, and none of these reads the bytes.
- **PV-F2 No cookie authority.** Every preview route answers a request with the workspace's session cookie exactly as it answers one without it.
- **PV-F3 Headers.** Every preview response carries the CSP of PV-2; the workspace's CSP names the preview origin in `frame-src` and nowhere else.
- **PV-F4 The bundle.** The viewer's bundle holds no workspace API client, and the PDF.js it carries is the pinned version.
- **PV-F5 Anchors.** Selecting text and copying an anchor gives the page's index and label, a box and the quote; a message from any other origin is ignored on both sides.

## Sources

D54 and D59 in the [decision register](../DECISIONS.md); the [threat model](../THREAT-MODEL.md) (T6, Z4); the refactoring plan's E-08 note; [PDF sources](pdf-sources.md); [research artifacts](research-artifacts.md) (RA-21) and the [artifact inventory](../ARTIFACTS.md).
