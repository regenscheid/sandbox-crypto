# Research foundations independent review

Three GPT-6.1-Sol reviewers independently inspected baseline `57e29536` through
`c294fadf`, read-only: storage/evidence, delegation/lifecycle, and worker/OpenShell
lifecycle. They found two P2 issues: incomplete evidence rows and unbounded field
names in the offline verifier; and durable Stop intent lost in live child cleanup
before the service polls. No actionable worker finding was reported. Live gateway
identity/restart behavior still requires integration evidence.

The original commit passed 6,230 tests across orchestrator, worker, workspace and
contracts; 51 live/integration or optional cases were skipped. Ruff and full
pyright passed. Evidence outside Git: `research-foundations-review-tests.log`,
SHA-256 `1be70fb3d3226e4bfb5e79db20588b3b3c7d12a647ee30de0ab2e988cce06515`.

## Evidence verifier fix

Export and verification share versioned field definitions. Verification rejects
unknown names before retaining them and requires every expected field at row end.
Both v1 and v2 remain supported. The storage reviewer checked the fix and reported
no remaining finding or regression.

Validation: 20 export, memory, starting-input and workspace download tests passed
in 10.61 seconds, including correctly checksummed empty/missing/unknown/duplicate
rows and rejection before reading further unknown fields. Focused pyright and
Ruff passed. Evidence: `research-foundations-review-export.log`, SHA-256
`a8f8e1b3de4786b0eab9718f5cf71b7fac2951909f9bffd2b1ada3ace74930c0`.

The Stop cleanup fix and its re-review are pending. Recursive Resume, complete
run-memory bounds, generated skill restrictions and orchestrator confinement
remain planned work. This review does not establish live research acceptance.
