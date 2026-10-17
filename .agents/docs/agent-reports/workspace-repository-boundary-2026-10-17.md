# Workspace repository boundary

Recovery and history routes now pass their existing DocumentStore to public
orchestrator repositories, as other workspace features do. Constructors unwrap
that same store inside the orchestrator; readonly flags, scope and explicit-user
writer authority are preserved. The architecture regression test remains unchanged.
The job-cancellation fixture now requires and verifies the exact execution context.
Control-table and API route inventories document all implemented recovery/history
interfaces.

Independent GPT-6.1-Sol review found no medium-or-higher issues, including import
cycles and readonly/writer ownership. Focused route, history, evidence, chat, jobs,
boundary and documentation checks: 32 passed in 4.13 seconds. Targeted Pyright:
0 errors, warnings or information. Ruff, formatting and diff checks passed.

Raw logs remain outside git:

- `/home/andy/.cache/pt/foundations-workspace-final.log`
- `/home/andy/.cache/pt/foundations-workspace-final-types.log`
