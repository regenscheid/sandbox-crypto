# Explicit tree recovery implementation

Local, unpushed implementation on `codex/research-foundations`. The coordinator
gate passed 48 tests in 17.79 seconds. The expanded recovery integration gate passed
132 tests in 30.74 seconds: three-level and repeatedly interrupted Resume, stopped/
completed branch preservation, unknown paid-child blocking, original tool and
question recovery, kernel generations, checkpointed child handbacks, stale cleanup
fencing, budget refusal and chat/annotation receipts. Full Pyright reports zero errors.

Evidence outside Git:

- `/home/andy/.cache/pt/foundations-coordinator.log`: SHA256 `afa2e81dacdc6c4f9cdfeb6bec939e227b9544b3d1d87772a3b2e3f5c7e363ec`.
- `/home/andy/.cache/pt/foundations-recovery-final.log`: SHA256 `bcf50f3f453b6a43ef30ada6b5b563bdcf1fa084e822d1886ee0c02afb415c89`.
- `/home/andy/.cache/pt/foundations-static-final2.log`: SHA256 `6d88a1b220adb7a3d62092b6e38431f0b3fe8babe9864fab90e5849766260332`.

Independent review identified and corrected scope, lease, kernel cleanup, model
alias, receipt replay, parent readiness, older-tree visibility and background-job
ownership issues. Durable child settlement and terminal-child kernel cleanup are
integrated. GPT-6.1-Sol re-review has no remaining medium-or-higher findings in this
scope. Actual OpenShell qualification is recorded separately; this report does not
claim scientific research quality or merged completion.
