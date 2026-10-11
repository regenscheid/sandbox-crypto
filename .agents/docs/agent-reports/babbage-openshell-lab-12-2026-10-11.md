# OpenShell v0.1.3 lab 12 on babbage, 2026-10-11: the orchestrator image and project records

This lab ran the harness from the new `orchestrator` image in its own OpenShell sandbox, then tore everything down. There the harness used its project records (W3) to have the worker create a project's default environment and a dedicated session environment. Like the earlier labs, this report records observed behaviour.

## What ran where

| | |
| --- | --- |
| Host | babbage: AMD Ryzen AI Max+ 395, Ubuntu 26.04.1 LTS, kernel 7.0.0-38-generic |
| Runtime | OpenShell 0.1.3, VM driver (KVM); sandboxes with 4 vCPUs, 4 GiB of memory and an 8 GiB overlay disk |
| Orchestrator sandbox | `orch-img`, from `sandbox-science/orchestrator:174e72e`, under `compile_orchestrator()`'s policy, which admits only the worker API, for `/opt/sandbox-science/bin/python3.14`, with `/opt/sandbox-science` read-only. It was ready 15 s after the request. Only the worker's token and two scripted model turns were uploaded |
| Research environments | Created by the worker from `sandbox-science/minimal:02f85b3`, under lab 10's policy |
| Services | Policy middleware, package cache and worker on the host, as in earlier labs |

## The run

The harness ran as `/opt/sandbox-science/bin/sandbox-science-harness`, as uid 1000. Calling it by its full path works because the VM driver does not apply the image's `PATH`.

| Step | Result |
| --- | --- |
| `project create --state-dir /sandbox/state --project p-img --profile minimal --policy sha256:0c70…` | The record, with default environment `p-img-default` |
| First `run`, session `s1` | **Failed**: "name exceeds maximum length (25 > 19)". See finding 1 |
| `run`, session `s1`, after the fix | The worker created `ss-p-img-d-59f6efab`. Shell (`hostname`, uid 1000, Python 3.14.8) and a Python cell (`42`) ran there at isolation tier 3, 17 s in all, environment creation included |
| `run`, session `s2`, `--dedicated` | The worker created `ss-p-img-s-ecce061d` from the base image (9 s). `../s1/marker.txt` was absent there: a dedicated environment does not see the default one's files |
| `session close`, session `s1` | `kernels stopped: python krn-s1-python`, and the record marks `s1` closed |
| `project show` | `s1` in `p-img-default`, closed; `s2` in `p-img-s2`, dedicated |
| Egress of the orchestrator sandbox | `https://pypi.org/simple/` and the package cache were refused (`Permission denied`). Only the worker API was reachable |

## Findings

1. **OpenShell limits sandbox names to 19 characters.**
   - Routable names must fit three segments and two `--` delimiters in one DNS label (`MAX_ROUTABLE_NAME_LEN`, `openshell-server/src/grpc/mod.rs`).
   - The worker's names (`ss-<ID>-<hash>`) passed only for IDs of up to seven characters. Every earlier lab happened to use one.
   - The fix, in [its own pull request](https://github.com/regenscheid/sandbox-crypto/pull/43), keeps names within 19 characters and never uses `--`. After it, both environments were created.
2. **The image is complete.** The harness, its project records and its worker calls needed nothing beyond the image, the token and the scripted turns.
3. **The image's prefix must be in the targets file.** `/opt/sandbox-science`, read-only, as `images/orchestrator/filesystem.yml` and docs/IMAGES.md say.

## Cleanup

- **Removed:**
  - the three sandboxes;
  - the units: worker, gateway, middleware and cache;
  - the lab directory (1.9 GB).
- **Checked afterwards:**
  - no lab processes;
  - the lab ports free;
  - no OpenShell paths under `~/.config`, `~/.local/state` or `~/.local/share`;
  - no driver socket directories in `/tmp`.
- **Kept:** the three images in the local Docker store.
