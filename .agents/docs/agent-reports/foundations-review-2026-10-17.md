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

## Stop cleanup fix

Cleanup resolves durable Stop ancestry for each child in one snapshot, preserving
branch and sibling distinctions even before the service polls or when a caller is
cancelled during the Stop write. Unstarted children are cancelled before the
read; repeated cancellation retains the read and fallback outcome writes. A read
failure still cleans up and then surfaces the failure. Unresponsive children
remain interrupted, with their requested Stop reason recorded.

Both race regressions failed on the original code and passed after the fix.
The delegation reviewer checked the fix, sibling isolation, pre-start behavior,
read failure and repeated cancellation; no remaining finding was reported.
The worker reviewer had no finding. All three reviews are complete, with no
critical, high or medium findings left in the reviewed changes.

Validation: 164 delegation, lifecycle, depth, restart, budget, harness, service
and failed-run tests passed in 33.57 seconds. Full pyright and Ruff passed.
Evidence outside Git:

- `research-foundations-review-stop-before.log` (the two expected failures),
  SHA-256 `67a7c001bbbb934f24ee79065aa0c60fa06fb04194b21153d59ba7470864771e`.
- `research-foundations-review-stop.log`,
  SHA-256 `7ac5648e85442fa97fd9ef6b309b8a102d8b01d85653be2019fa8bf17cf6327c`.
- `research-foundations-review-pyright-final.log`,
  SHA-256 `6d88a1b220adb7a3d62092b6e38431f0b3fe8babe9864fab90e5849766260332`.

The 6,230-test run preceded the fixes; the 20 export and 164 lifecycle checks
cover their changed paths afterward. Recursive Resume, complete run-memory
bounds, generated skill restrictions and orchestrator confinement remain
planned work. No live service was changed, and this review does not establish
live research acceptance.
