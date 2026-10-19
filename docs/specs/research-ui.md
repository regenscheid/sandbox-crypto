# Research workspace interaction

The workspace keeps project/session navigation on the left, chat in the center,
and an optional research pane on the right. This is the owner's 2026-10-17 UI
direction. The existing Svelte app, authenticated API, document revisions,
explicit Resume, and sandbox execution boundaries remain authoritative.

- Navigation is collapsible, with project selection and session search. On small
  screens it is a drawer. Existing deep links continue to work.
- Documents, Plan, Agents, and Activity are available from the session header.
  The right pane can be hidden or resized with pointer and keyboard. Hiding it or
  switching tabs preserves mounted editors and drafts. Documents are scoped to
  the project and current session; opening one keeps chat visible.
- The desktop research pane can expand to 50% of the browser window, using its
  drag handle, keyboard, or a half-window button. Smaller screens retain the drawer.
- Documents has a library with search, scope and sort controls above its rows.
  Uploaded originals and research artifacts remain reachable from the library.
  Open documents appear as named, closable tabs beneath Documents. Switching or
  closing a tab keeps its editor and unsaved draft available when reopened.
- Agents shows observed parent/child runs, status, delegation brief, and live
  activity. Failed observations are visibly stale, never presented as current.
- Chat accepts file selection and drag/drop, removable attachments, explicit
  upload status, and retryable failures. The file limit is 100 MiB; originals have
  2 GiB/project and 8 GiB/store retention quotas. Bytes are never put in browser storage.
  Uploads are attributed to the user and retained without starting a research VM.
  Explicitly sending attachments prepares the session's configured environment
  through the worker, under the project's profile and policy, before delivering
  copies and queuing the message. A preparation failure retains the originals
  and leaves the message unqueued for retry. Files are delivered through the worker API;
  parsing or executing research files never happens in the workspace process.
- Chat and documents share the existing Markdown/KaTeX renderer, including
  inline/display dollar and backslash delimiters, safe links, and project macros.
- Existing plan editing, recovery, reviews, jobs, kernels, and history remain
  reachable. Display changes do not start models or resume interrupted research.

OpenScience (`synthetic-sciences/openscience`, Apache-2.0, revision
`722ad051b7f601bab9e33749aedf942faf8c4737`) supplies interaction references:
its session sidebar, optional inspector, keyboard resizer, and attachment cards.
The Svelte components are implemented here against this project's contracts;
no upstream runtime, prompts, skills, icons, or font assets are imported.

The [workspace browsers proposal](workspace-browsers.md) extends the accepted
ledger direction to Skills, MCP services, Overview, Memory and session panes.
Its connected prototype is for review; production integration remains pending.
