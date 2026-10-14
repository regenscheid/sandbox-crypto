# OpenShell backend conformance for both research profiles on babbage

Run on 2026-10-12. The unchanged external worker conformance suite passed all
23 cases on each of the `minimal` and `crypto-base` linux/amd64 profiles. Both
managed MicroVMs retained effective and declared isolation tier 3. This closes
the pending external backend conformance/probe and linux/amd64 research-profile
acceptance checks; arm64, Kubernetes and multi-platform acceptance remain open.

The retained evidence (archived outside git, sha256 `b381bb770c0a`) contains
20 exact text records, including per-profile pytest/JUnit results, capabilities,
independent guest/SQLite state, setup bookkeeping and teardown. Its 77,108 bytes
have SHA-256 `b381bb770c0ae5702a4c98ac90012184c6e8289d0fccabfd0e1773ff227ee779`.
All embedded lengths and hashes match the separately captured originals. Root's
scans against the actual disposable worker/client tokens passed; no credentials
are retained.

## Exact source and environments

| Component | Configuration |
| --- | --- |
| Host | babbage, linux/amd64, Ubuntu 26.04; development Python 3.13.15 |
| Worker, middleware and cache | Frozen main `73c07aabc2c00263abcb521dc202e9fe6ebe955e` |
| Test source | Unchanged `packages/worker/tests/test_conformance.py`, SHA-256 `3b1e64c5f85a1e564869178d03df9ff3c4f15af74ca4bb17194fa686e66195c5` |
| Gateway | OpenShell 0.1.3, MicroVM driver, loopback mTLS gateway |
| Profiles | `sandbox-science/minimal:02f85b3` and `sandbox-science/crypto-base:02f85b3` |
| Project | Synthetic `conformance`, with separate managed environments/sessions |
| Services | Four disposable units ran: gateway, middleware, cache and worker |
| Credentials | Synthetic provider and scoped worker client |

| Profile | Environment / immutable sandbox ID | Guest boot ID |
| --- | --- | --- |
| minimal | `lab23-minimal` / `fc88176d-0265-4eb0-930d-7a4e676e9411` | `18dff5bc-314b-41cf-be7f-5bd07f6a80ad` |
| crypto-base | `lab23-crypto-base` / `45c5917b-db79-40e1-b173-a3b5018072c5` | `7bb5acf3-7566-41c1-b4a7-9970485966e6` |

Both actual guest interpreters were Python 3.14.8. Each environment's capabilities
reported a separate kernel, hidden host sentinel, seccomp filter, `no_new_privs`,
zero capabilities and no isolation-probe findings. Its effective tier remained
the declared tier 3. Independent post-checks observed the same guest boot IDs.
These are the probe's observed properties, not a general VM escape assessment.

## Results and coverage

| Profile | External cases passed | Intentionally skipped local variants | Pytest elapsed |
| --- | ---: | ---: | ---: |
| minimal | 23 | 23 | 21.93 seconds |
| crypto-base | 23 | 23 | 22.47 seconds |
| Total | 46 | 46 | Separate runs |

JUnit recorded no failures or errors. The local variants are intentionally skipped
when `SS_CONFORMANCE_*` selects an already running external worker; they are not
missing external cases. The existing suite and every assertion/timeout remained
unchanged. It checked shell/argv, reported location/isolation, cancellation/output
bounds/timeouts, Python kernel state/errors/streaming/interrupt/restart/death,
queued cancellation, event replay, publication, file preconditions/shared conflicts,
confinement, idempotency, authentication and unknown execution lookup.

The independent SQLite inspection found 31 terminal executions per environment:
21 succeeded, four failed, four cancelled and two timed out. Those negative states
are expected by the passing exception/cancellation/timeout cases. Ten artifacts
were retained in total. The publication cases check the existing recent-path
`produced_by` hint; that field remains unproven actual writer/producer attribution,
and the suite establishes no observed-input lineage.

Crypto-base advertised both Python and Sage, but this conformance suite executed
Python cells only. It adds no new Sage, G6K, lattice, TeX or PDF functional assertion.
Earlier image/tool acceptance remains separately dated. This run exercised both
worker profiles with their compiled filesystem settings through the actual VM
backend; it did not rerun every plain-container image check.

## Bookkeeping and scope

An initial export loop failed after successfully creating both environments because
it added capability records while iterating its dictionary. The corrected export
inspected those existing environments. The failure and both scripts are retained;
no product fix, environment recreation, test change or relaxed assertion resulted.
A broad secret-prefix scan initially matched its own literal source; the corrected
actual 16-hex token pattern and scans for the real disposable tokens passed.

This lab used frozen main `73c07aa`, not the bounded capture branch. The capture
prerequisite's separate [Lab22](babbage-openshell-lab-22-2026-10-12.md) used exact
`b535856`. Lab23 does not extend Lab22's capture fault/ownership evidence or claim
automatic output inventory. It also proves no arm64, Kubernetes, other backend,
multi-platform registry or macOS parity. No source, runtime image, persistent
configuration, assertion or timeout changed for this acceptance run.

## Cleanup

The gateway reported zero sandboxes, and teardown removed 14 GiB of disposable
state. Independent verification at `2026-10-12T12:46:22.698738Z` found all seven
possible lab unit names inactive, no lab processes, no occupied lab ports, no
OpenShell XDG paths, no driver socket directories and no lab state directory.
Both owned pytest fixture roots were removed. Shared Docker images and unrelated
state were preserved. Main and the test source stayed frozen through teardown.
