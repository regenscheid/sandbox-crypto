# Bounded artifact capture in OpenShell on babbage

Run on 2026-10-12 against the exact independently reviewed, signed worker source
`b5358567756da1c9f3086b6f1645a9b089e0abd8`. Explicit publication retained exact
binary and empty content, promptly refused oversized and special sources, and
left healthy execution and ordinary file access available. The disposable lab
was completely removed before any subsequent source-tree changes.

The retained evidence (archived outside git, sha256 `3474c2a6577e`) contains
16 exact UTF-8 text records: API checks, independent guest/SQLite inspection,
source/settings identities, preflight, service logs and teardown verification.
Its 103,849 bytes have SHA-256
`3474c2a6577ecd4b6089e4045c038175afae2e580e0fbfe8bb21ce2d1065503d`.
Every embedded record's length/hash matches the separately captured original.
Root scanned the retained records against the disposable credentials; none are
retained. The reusable check is [`artifact_capture.py`](../../labs/artifact_capture.py).

## What ran where

| Component | Configuration |
| --- | --- |
| Host | babbage, Ubuntu 26.04; development Python 3.13.15 |
| Worker | Signed clean `b5358567756da1c9f3086b6f1645a9b089e0abd8` |
| Middleware and cache | Main `73c07aabc2c00263abcb521dc202e9fe6ebe955e`, containing merged CP13 PR #85 |
| Gateway | OpenShell 0.1.3, MicroVM driver, loopback mTLS gateway |
| Sandbox | `sandbox-science/minimal:02f85b3`, Python 3.14.8 |
| Scope | Synthetic project `lab22`, environment `lab22-capture`, session `capture` |
| Managed identity | `ss-359030333a66e605`, immutable ID `0629499f-bf6d-416c-8517-ad98b29f21ed` |
| Guest boot | `c6400b2f-f06f-4c35-8e7f-c148baf06ae1` |
| Capture limit | Disposable worker `max_artifact_size=1048576` (1 MiB) |
| Services | Four disposable units ran: gateway, middleware, cache and worker; synthetic credentials |

The lab helper SHA-256 was
`9bedec7e612ae15180ea4e716045a65abed07a66f7be745c5687d33ec65e30d5`;
the worker capture module SHA-256 was
`e9c053973f874b51e669148552a32ee67aee6979c8320f9e3ffa87f98fb5441b`.
Root stopped only the disposable worker to set the 1 MiB limit, then restarted
that unit before creating the test environment. The production 1 GiB default,
runtime images, persistent services and security settings were unchanged.

## Observed API and state results

The helper published 24 binary bytes containing CRLF, NUL, `0xff`, a backslash and
a quote. Downloaded content exactly matched the source, size and SHA-256
`2c04d3c9ed9946f60f526f027e7e2e22f7d78e5bb14e7c25d7810f7378f230dc`.
An empty file retained zero bytes and the standard empty-content SHA-256. The
ordinary file-read response also retained the exact binary bytes and hash.

Three rounds each tried four invalid sources, followed by another successful
exact binary publication. All twelve attempts returned HTTP 400 `invalid_request`
in 0.10665–0.14012 seconds.

| Source | Guest inspection | Refusal |
| --- | --- | --- |
| `regular-too-large` | Regular file, 1,048,577 bytes | Source exceeds byte limit |
| `sparse-too-large` | Regular sparse source, 1,073,741,824 bytes | Source exceeds byte limit |
| `link.bin` | Symlink | Capture refused |
| `pipe` | FIFO | Source is not a regular file |

The first retained artifact still downloaded unchanged after all failures and
later publications. A final shell execution succeeded and printed
`healthy after refusals`. The independent post-check found five artifact records
(four exact binary publications and one empty publication), three terminal shell
executions, zero capture leases, zero capture helper processes and zero unreadable
guest command lines. This verifies normal helper/lease completion for this run.

The returned `produced_by` attempt IDs remain unproven recent-path hints. They do
not establish the actual writer, exclusive producer, observed inputs or a complete
output manifest. The helper's exact byte/hash checks establish retained content;
they do not prove atomic mutable-path capture or closed writers.

## Local validation and its limits

The source passed 60 capture/lifecycle cases and the complete local suite:
9,592 Python tests, 43 opt-in skips and two existing serializer warnings; 436 web
tests; and 24 post-build math checks. Locked uv/npm installation, Python/npm
licenses, Ruff formatting/lint, strict Pyright, Svelte/TypeScript checks, generated
protocol consistency and both production builds passed. Independent adversarial
review cleared the source and helper. The subsequent main ancestry and acceptance
documentation merge changes no product, test, configuration or helper bytes, so
these full-gate results apply to the unchanged source. Fresh PR CI remains the
merge gate.

Lost bridge/reply, generic error without cleanup proof, blocked host threads,
delayed spawn, repeated cancellation, pipe-drain failure, corrupt leases and
wrong original-VM identity were local hostile tests, not faults induced in this
OpenShell run. This lab did not prove a hard deadline for unkillable filesystem
I/O, maximum-size successful capture, concurrent-writer atomicity, legacy-peer
behavior or arbitrary process ownership. Actual helper/host work remains owned
until completion; unknown cleanup retains durable admission.

Automatic execution-output inventory, producer/input lineage and LD-49 writer
proof remain separate follow-ons. No automatic publication or execution exemption
is enabled by this prerequisite.

## Cleanup

The gateway reported zero sandboxes. Teardown removed 1.1 GiB of disposable state.
Independent verification at `2026-10-12T12:36:45.934438Z` found all seven possible
lab unit names inactive, no lab processes, no occupied lab ports, no driver socket directories,
no lab state directory and no OpenShell XDG paths. The tested source and helper
stayed frozen throughout the run and teardown. Shared images and persistent
configuration were preserved.

## Addendum: GitGuardian alert investigation, 2026-10-12

The user supplied a Bearer Token alert location in commit `cd1f86f`,
[archive line 36](https://github.com/regenscheid/sandbox-crypto/blob/cd1f86f098b23b8ce37ea0e9f45b01254ffe5c19/.agents/docs/agent-reports/babbage-openshell-lab-22-evidence-2026-10-12.json#L36).
That location ends the `gateway.log` record; its encoded text is on line 35.
Decoding it locates ordinary diagnostic line 49:
`renewed gateway sandbox JWT sandbox_id=<UUIDv4>`. The value is the already-recorded
managed sandbox UUID, not token bytes. Root and independent review checked both
the literal and JSON-encoded record.

The pinned [OpenShell v0.1.3 source, lines 283–288](https://github.com/NVIDIA/OpenShell/blob/v0.1.3/crates/openshell-server/src/grpc/auth_rpc.rs#L283-L288)
logs only `sandbox.sandbox_id` at this point. The inspected source has Git blob
`848adae6f594b1d6cf5ef9dbe7b55679bffcc035`. The JWT is returned separately in
the response; the log statement does not emit it. GitGuardian's official
[generic Bearer detector documentation](https://docs.gitguardian.com/secrets-detection/secrets-detection-engine/detectors/generics/bearer_token)
includes JWT among recognized authentication terms and states that this detector
has no validity check. This strongly supports a false positive at the supplied
location; the exact proprietary match and incident payload were not independently
available, so they are not claimed reproduced.

No live credential was identified in this record. The original Lab22 archive
remains byte-identical at 103,849 bytes and SHA-256
`3474c2a6577ecd4b6089e4045c038175afae2e580e0fbfe8bb21ce2d1065503d`.
No credentials were revoked, integration installed or published history rewritten
as part of this investigation. This dated addendum changes only the report and
its inventory entry; it does not redact or replace the original evidence.
