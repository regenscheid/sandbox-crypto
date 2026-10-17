# OpenShell orchestrator qualification — 2026-10-17

## Qualified revision and setup

The normal launcher passed the disposable runtime scenario on product revision
`49f5fde0296b63b41f53a9f73e7e1b4c03b70982`, using image
`sandbox-science/orchestrator:confinement-ultimate-c8ff08ec` (also tagged
`sandbox-science/orchestrator:49f5fde0`), digest
`sha256:816476d293b300ab71eebf35252727b66c54eedba3e89c12b8d3dd737f848763`.
The image was 130 MB and built in 6 seconds. Official OpenShell v0.1.3 binaries
were checked against release digests. The actual VM driver, separate research
sandbox, trusted worker/control service and workspace used the production launcher.
Gateway, middleware, cache, synthetic model endpoint, TLS and state were disposable;
no personal service or real provider credential was used.

## Results

One driver admitted a parent and delegated child, waited for the child's actual
worker shell execution to be **running** and its parent to be waiting, then deleted
orchestrator VM `9b0398ab-d996-4515-9d24-30e99b8f845c` during execution. Explicit
replacement created VM `3e424d7d-092e-4425-9310-1cbae90ab2da`. Worker, control and
workspace PIDs stayed unchanged; the runtime used the separate VM with no host
agent-loop fallback. Replacement made **zero model calls before explicit Resume**.
The original input remained delivered to its original root, with no duplicate root.

Explicit Resume `resume-57c841039dac417df0a398dd` completed root
`20261017T102029855460Z-run-d4289a4eaac3b522b7171071` and its child. Assertions proved:

- Four attempts retained predecessor links; the child continued before its parent.
- Exactly one original shell intent remained, with the same operation identity.
- Canonical reconciliation retained the original successful execution: exit 0,
  exact `final-resume-checkpoint` stdout, 23 bytes, untruncated, at least 11 seconds
  elapsed. The worker execution was observed running before VM deletion.
- No paid model intent remained unknown. All five final synthetic model calls
  authenticated successfully; no credential value was recorded in observations.
- The replacement denied public HTTP with EACCES, had no synthetic host credential
  path or Docker socket, and rejected unauthenticated worker readiness with HTTP 401.

An initial denial probe addressed VM-local loopback incorrectly; its connection
refusal is retained separately. The corrected `host.openshell.internal` probe passed.
Earlier scenarios exposed and motivated the original-input reconciliation fix;
those failures are not counted as qualification of the final revision.

## Cleanup and evidence

Cleanup checked the isolated gateway against all **10 recorded immutable sandbox
identities**, confirmed an empty sandbox list, stopped all five unique transient
units, verified all eight allocated ports closed and found no remaining lab process.
The isolated lab directory, tokens, TLS and durable state were removed. The final
qualified image and inert downloaded tools/source and image caches remain available;
they are not running services, sandboxes or mounted research state.

Raw evidence stays outside git in `/tmp/ss-confinement-evidence-c8ff08ec`.
No token, TLS private key, cookie or project database was retained. SHA-256:

| Evidence | SHA-256 |
| --- | --- |
| `final-resume-evidence.json` | `06a6359b694db2e023f0befdcbf780d217952715d14091905ef5949c20820bd2` |
| `cleanup.json` | `4ece6721456f280237cf09a238f83a0389693d7005641235aebf30ce87176ad8` |
| `ultimate-denials.log` | `06a17df9060492fed658c9f01b4fc4943a4e65770afa9608ab416954b361e888` |
| `auth-observations.jsonl` | `b298ce767eff12baa0f7ebb2dc515b4f29e183e3aa0c896dc05f7e05b28c06ed` |
| `sha256.json` manifest | `596aad0f70d064b088ea6ff7b1b653072cf3df06e41e7aed51f733672335804a` |

## Scope

This establishes actual OpenShell placement, denial, worker-effect lifetime,
replacement, durable tree recovery and verified teardown for this scenario.
Scripted main/delegated model responses do not establish research quality, real
provider billing or optional reviewer/watcher/embedding/MCP live integrations.
A real gateway key still needs provider-side alias and budget scoping. Lost-kernel
and uncertain-create cases remain separately covered by deterministic checks;
this shell scenario does not claim their live qualification. The repository-wide
suite is coordinated separately and is not a result of this lab.
