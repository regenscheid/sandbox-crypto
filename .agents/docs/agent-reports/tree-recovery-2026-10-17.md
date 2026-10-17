# Explicit tree recovery implementation

Local, unpushed implementation on `codex/research-foundations`. The coordinator
gate passed 48 tests in 17.79 seconds: three-level Resume, stopped/completed branch
preservation, unknown paid-child blocking, stale cleanup fencing, and existing
harness/service/annotation-request behavior. Source Pyright reports zero errors.

Evidence outside Git:

- `/home/andy/.cache/pt/foundations-coordinator.log` (hash recorded below in the final acceptance update).
- `/home/andy/.cache/pt/foundations-recovery-types-4.log` (hash recorded below in the final acceptance update).

Independent review identified and corrected scope, lease, kernel cleanup, model
alias, receipt replay, and parent readiness issues. The durable child settlement
increment remains under implementation; this report does not claim live OpenShell
acceptance or merged completion.
