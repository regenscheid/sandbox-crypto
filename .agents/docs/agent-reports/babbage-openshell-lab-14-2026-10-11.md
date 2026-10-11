# OpenShell v0.1.3 lab 14 on babbage, 2026-10-11: clients limited to projects, and hosts admitted ahead of use

This lab ran the worker with client tokens limited to projects (D78) against a real gateway, and had the harness admit a host before its code contacted it (D69, ahead of use). Then it tore everything down. Like the earlier labs, this report records observed behaviour.

## What ran where

| | |
| --- | --- |
| Host | babbage: AMD Ryzen AI Max+ 395, Ubuntu 26.04.1 LTS, kernel 7.0.0-38-generic |
| Runtime | OpenShell 0.1.3, VM driver (KVM); sandboxes with 4 vCPUs, 4 GiB of memory and an 8 GiB overlay disk |
| Worker | From the `impl/pre-admission` branch (which includes D78), on the host, with `[openshell.environments]` and two clients: `orchestrator-lab14` for project `lab14` and `orchestrator-lab14b` for `lab14b`. Their tokens came from `sandbox-science-worker token --out`, which printed the `token_sha256` lines the settings took |
| Policy | `examples/policy.yaml`, stored with `policy add --project lab14`, so only `lab14`'s client (and the pool's token) could use it. Its provider `github-readonly` was registered from the endpointless `github-token` profile with a synthetic token |
| Harness | On the host, with `lab14`'s client token, its project records in a state directory and two scripted model turns |
| Research environment | Created by the worker for `lab14` from `sandbox-science/minimal:02f85b3` |

## Results

| Check | Result |
| --- | --- |
| Creating the environment with the client's token | `lab14-default-2fcadb95a3fc`: the harness's ID with its random tag, in a sandbox with a random name, `ss-99035f4b0300233b`. OpenShell accepted the 19-character name. The first attempt failed with 503 until the policy's provider was registered, as designed |
| Capabilities | The `admission` feature, so the harness offered the agent `admit_hosts` |
| `admit_hosts ["www.iacr.org", "pypi.org"]` | `www.iacr.org:443 reachable`; `pypi.org:443 refused: … a package registry, reachable only through the package cache` |
| The next tool call | `https://www.iacr.org/` connected at once (0.2 s), with no refused first connection. `https://csrc.nist.gov/`, not admitted ahead, failed at once with `Permission denied`, as on-demand admission still works |
| Timing | The worker logged the admission 13 ms before it accepted the next execution: `openshell policy set --wait` returned only once the sandbox could reach the host |
| Client `lab14b` against `lab14`'s environment | Get, capabilities, admissions and a submission naming `lab14b`: `404 not_found`. A submission naming `lab14`: `403 policy_denied`. Creating an environment for `lab14`: `403`. Creating one for `lab14b` under the revision granted only to `lab14`: `404`, as for a revision never stored |
| The pool's token | Creating `lab14`'s ID for `lab14b`: `409 conflict`. Reading the environment: `200` |
| Audit lines | `POST /v1/environments/{environment_id}/admissions by orchestrator-lab14: 200`: the route, the client and the status, never the path a client sent |

## Findings

1. **Admission ahead of use works on a real gateway.** A host the agent named before running code was reachable on the first connection, because `policy set --wait` waits until the sandbox has the new policy. A batch costs one policy reload, before any transfer starts.
2. **Clients limited to projects hold on a real gateway.** Every cross-project request was refused, the ungranted revision looked like an unknown one, and the owning client and the pool kept their access.
3. **Random sandbox names work with OpenShell.** `ss-` and 16 hex digits fit its 19-character limit.
4. **Some sites refuse Python's default User-Agent.** `www.iacr.org` answered `403` with Cloudflare's `error code: 1010` to `Python-urllib`, and `200` to another User-Agent. The policy middleware allowed the request: the refusal was the site's. Agents fetching with urllib should set a User-Agent.

## Cleanup

- **Removed:** the sandbox, the provider and its profile, and the units: worker, gateway, middleware and cache. The lab directory (1.1 GB) was removed.
- **Checked afterwards:** no lab processes or units, the lab ports free, no OpenShell paths under `~/.config`, `~/.local/state` or `~/.local/share`, no driver socket directories in `/tmp`.
- **Kept:** the worker's log and the harness's output, which hold no tokens, in the session scratchpad; the images in the local Docker store.
