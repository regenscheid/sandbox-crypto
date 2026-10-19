# Durable work ownership increment

Scope: schema62 work/attempt identity, explicit Resume request storage, transactional
write fencing across async/thread boundaries, parent completion checks, and atomic
consumption identities for outcomes returned by tools. Recursive recovery and worker
fencing remain subsequent increments; this is not end-to-end Resume acceptance.

Validation:103 affected tests passed; scoped pyright and ruff passed. GPT-6.1-Sol
independent review found first-call ownership and unconsumed/cross-root outcome gaps;
regressions and final re-review addressed all findings in this slice.

Raw evidence outside Git:
- `foundations-resume-first.log`: `d8dc7ee8ddb6f3bb41693e819caf0b4d5fd23cfbf4e5ded0888ea87f43b87bbc`
- `foundations-resume-types.log`: `6d88a1b220adb7a3d62092b6e38431f0b3fe8babe9864fab90e5849766260332`

No live services, real gateways or OpenShell runtimes were exercised.
