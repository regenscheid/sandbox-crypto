# Generated skills restriction implementation

Prepared and validated locally on 2026-10-17; integrated as `a44cec87` and
`a6d2abbf`. No new live worker/image deployment or OpenShell/image-build claim.

The image and executor share a static installed-source scanner for Conda, pip and
system package ownership. Canonical inventories retain package/version, relative
source, source hash, extraction rule and format/extractor version. Literal reads,
ELF evidence and maintained fallback families supplement unconditional existing
denials; computed/unsupported/oversized sources have explicit coverage notes.
Enumeration/aggregate-bound/read failures refuse authoritative publication.
No installed tool, module or pack helper executes during extraction.

Executor publication binds the package fingerprint/epoch and rechecks it after
scanning. Provisioning invalidates that state; shell/job/cell launch admission
checks the current digest, staging pins it, and refreshing one pack revalidates all
loaded packs. Surviving kernels retain their birth digest and require restart on
inventory change. Missing/stale coverage denies bindings while legitimate ordinary
variables remain allowed. Cached disk JSON is never treated as authoritative.
The boundary is serialized launch admission, rather than process-lifetime locking.
Compatibility is worker protocol8/executor3, with no storage migration.

Evidence (raw logs remain outside Git under `/home/andy/.cache/sandbox-science/`):

| Log | Result | SHA-256 |
| --- | --- | --- |
| `skills-final-tests.log` | 307 passed, one staging refresh fixture failed; corrected and rerun below | `7a34dc05f3db1e37f8bbae61d841dac1268b719d987b5e5c1f0e220836a69b30` |
| `skills-staging-final.log` | final staging6/6 pass | `279a495bd9c6cf723e821b935cc74dc1f2abf0766398683e5e6994dd3fcd94b7` |
| `skills-final-pyright.log` | zero errors/warnings | `6d88a1b220adb7a3d62092b6e38431f0b3fe8babe9864fab90e5849766260332` |
| `skills-api-check.log` | generated API check exited0 (empty output) | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |

Independent coordinator review found no remaining medium-or-higher issue in the
scanner, exact denials, digest refresh, all-pack revalidation or surviving-kernel
checks. Normal final lab checks still own image-build/OpenShell runtime validation.
