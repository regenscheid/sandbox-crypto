# Creation cleanup continuity

Local implementation on `codex/research-foundations`; no live OpenShell gateway or
personal service was started. Worker protocol remains 7 and control schema remains 58.

The worker now retains an environment's original creation record and first observed
sandbox ID until identity-checked deletion is confirmed. A retry cannot replace that
record first. Failed or pending deletion, gateway loss, worker restart, changed IDs
and repeated cancellation retain ownership. An unknown creation with no observed
identity remains blocked: absence does not prove a disconnected request cannot
create the sandbox later. A late appearance can be reconciled by the next retry;
otherwise operator reconciliation is required.

Validation: 181 tests passed in 25.48 seconds across environments, OpenShell CLI,
recovery gates, isolation and launcher sandbox tests. Added cases cover failed and
pending cleanup across restart, a late appearance, same-name replacement refusal,
and cancellation while cleanup owns the environment lock. Ruff and pyright pass.
These are fake-CLI and local fixtures, not real VM acceptance or tree Resume.

Evidence outside Git: `research-foundations-cleanup2.log` in the local test evidence
directory; SHA-256
`e616e51930aecc5c6a51dd546fc0a7b1fc757a0803a441d14135be310be28ca1`.

Related local checks: parent waiting passed 122 tests in 29.76 seconds
(`research-foundations-parent-wait3.log`, SHA-256
`0fc9f60bb8fae1d9647a9850fc0e8e308a4fdf4cb0ca3be20c71c7f21f0b1aa0`);
the export's scoped-store and API-table fixes passed 41 targeted tests
(`research-foundations-export-boundary.log`, SHA-256
`ab6e9a53baa4f47d2e6907d6536da91818f9157b7c7a60cd188b2b2b708d5dd8`).
The preceding full orchestrator/workspace run had 4,558 passing tests and two failures
in those boundary/document checks; both were fixed. Independent review, integration
and combined live continuity acceptance remain pending.
