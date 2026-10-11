# OpenShell v0.1.3 lab 6 on babbage, 2026-10-11

Runtime validation of worker-managed environments (D73): the worker service created, revised and deleted a research sandbox against a real gateway. The lab was torn down afterwards. Like the earlier labs, this report records observed behaviour.

## Setup

- **Services.** As in labs 2 to 5: gateway (VM driver), policy middleware and package cache on loopback, in transient systemd user units.
- **Provider.** `github-readonly`, with the endpointless `github-token` profile and a synthetic token.
- **Worker.** It ran with an `[openshell.environments]` table that pointed at the middleware's state directory, the lab's targets file and the pool ceiling. Its one profile, `minimal`, mapped to `docker.io/library/python:3.13-slim`, and the executor ran with that image's Python.
- **Policies.** `sandbox-science-worker policy add` stored three revisions and printed each one's compiler findings:
  - the example policy;
  - the same with one more rule;
  - a copy naming a provider the gateway did not have.

## Results

| Check | Result |
| --- | --- |
| Create | The sandbox `ss-env-a-77e122ad` was ready 11.3 s after the request, with the provider attached and the middleware binding written. Repeating the request returned the same environment without a second sandbox |
| Executor | It ran in the new sandbox, reporting tier 3. Inside, `/dev/shm` was writable and `GITHUB_TOKEN` held an OpenShell placeholder, not the token. The package cache answered 200 and `pypi.org` was refused |
| Revision | `openshell policy set --wait`, carrying the live filesystem section, took 9.2 s. The binding then named the new revision, and shell calls kept working |
| Unregistered provider | Creating an environment under the third policy was refused as `unavailable` before any sandbox was made |
| Delete | The sandbox and its binding were removed, and the worker no longer knew the environment |

## Findings

- **Revisions must carry the filesystem section.** An earlier check the same day showed this. OpenShell refuses a revision without the sandbox's filesystem section ("filesystem policy cannot be removed on a live sandbox"). It accepts one that carries the section `openshell policy get --base` reports. It also accepted a section without its own baseline paths and stored it so, although running processes kept their Landlock rules. The worker therefore always sends the live section.
- **Error shapes.** `openshell sandbox get` of an unknown name exits 1 with "sandbox not found". `sandbox delete` of a deleted sandbox exits 0. A create naming an unknown provider exits 1 with "provider '…' not found". The worker's fake CLI in `test_environments.py` follows these.

## Cleanup

- **Removed:**
  - the sandbox and the provider;
  - the units: worker, gateway, middleware and cache;
  - the lab directory (501 MB).
- **Checked afterwards:**
  - no lab processes;
  - the lab ports free;
  - no OpenShell paths under `~/.config`, `~/.local/state` or `~/.local/share`;
  - no driver socket directories in `/tmp`.
