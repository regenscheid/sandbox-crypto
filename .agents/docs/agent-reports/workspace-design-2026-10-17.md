# Connected workspace design review — 2026-10-17

Scope: Local Codex prototype extending the owner-accepted ledger direction to
Skills, MCP services, Overview, Memory, Settings and session panes. Synthetic
records only. Production Svelte screens and APIs are unchanged by this pass.
The [interaction proposal](../../../docs/specs/workspace-browsers.md) maps current
contracts and missing MCP inventory/observation data.

## Result and validation

- Skills uses a filtered catalog, source inspection and explicit loading-rule
  review. MCP separates configuration, policy and dated observations, with
  searchable read-only tools. No connection or permission changes are offered.
- Local Codex reported 34 model/DOM-event checks and 48 static screen-state
  checks before the targeted corrections below. Those are not browser tests.
- Host browser checks at 1280 × 720 cover search, filtering, selections, Skills
  draft retention and conflict/rebase/save, MCP schemas and unknown observations,
  Memory before/after, Plan phases, Agents stale observations and Activity filters.
  Overview had no horizontal body overflow. The final half-window pane measured
  640 px; closing it restored chat to the whole 1,070 px content area.
- A final adjustment removed the half-window's desktop pixel cap. Independent
  review found false success feedback on failed browser storage and skipped
  native disclosures in mobile focus containment. Both were corrected and
  independently rechecked. A focused synthetic failure/recovery check confirms
  storage failure keeps the in-memory draft and exposes the warning; final
  JavaScript syntax checks pass. Mobile rendering/native keyboard behavior
  remains unverified in a browser.
- Existing ledger HTML/notes and brand specification hashes are unchanged. The
  generator separately reported all 22 pre-existing project files unchanged.
  This work did not install dependencies, start research, or contact MCP services.
- The local design preview opens on Skills. Its actions affect browser fixtures,
  not project settings. The production Skills API can supply most of the proposed
  view; MCP requires a sanitized, authenticated project inventory and attributed
  historical metadata contract before integration.

## Evidence

Local evidence bundle: `workspace-design-20261017`, outside git. The design
project retains `workspace-redesign.html` and `workspace-design-notes.md`.

| File | SHA-256 |
| --- | --- |
| `workspace-redesign.html` | `ca98ffc7344f4cc996eca396a5c784846f820d06d83dd4210ae78079577c3b7c` |
| `workspace-design-notes.md` | `a68e91d1d05657d47a84ae47802ad9b23248dca804e2aea964eb8518404710f8` |
| `skills-proposal.png` | `0e1ef5ffe39a53387bd92d4245a9a18da5b0ef2fd1594e422c2fb9ebcd6dfa7b` |
| `mcp-proposal.png` | `e42cca627e5d945bfd5948e312418bf15561136c714961860c674e876a724286` |
| `browser-checks.json` | `4abcb70cf81548ca6ffbd6c5124fb7ecf27361496f064a240f1ba30d7a6e5c23` |
| `storage-failure-check.json` | `9a377d00b99e623210d08ab50605956f7f90133e026882b3d9d94614f9367d49` |
