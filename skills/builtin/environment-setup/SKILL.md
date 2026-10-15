---
name: environment-setup
description: Installing software and reaching network hosts from the sandbox (capabilities and packages through the provisioning tool, the package cache, admit_hosts) and what to do when the policy refuses or asks for approval. Use it before installing anything or downloading from a new site.
---

# Setting up the environment

## Shared and dedicated environments

Each session runs in an environment. By default that is the project's shared environment: every session there has its own kernels and workspace, but all of them share the installed software. A session opened as dedicated has an environment of its own, made from the same base image, with its own packages and history. An install in the shared environment changes it for every session that uses it, so install what the work needs and nothing more. A provisioning request can name another of the project's environments; the session's own is the default. The project's shared area is kept in step across all of its environments.

Every change to an environment's packages is a new revision, recorded with a lockfile, and each execution reports the revision it ran under. A result can therefore name the software it came from.

## Installing with provision

`provision` installs through the platform, rather than through pip or conda run by hand. It resolves the request in the sandbox, checks every package file against the project's policy, asks for permission when the policy says to, installs in the environment's turn, records the change, and tests the result. A request gives its purpose and one of:

- a capability from the platform's catalog, by name, such as gp, gap, sage, sympy, fpylll or python-flint. A probe tests it, and one that already works is left alone;
- conda-forge package specs, such as pari>=2.15;
- PyPI requirements, only for what conda-forge lacks, with the reason, in a request of their own after the Conda one. They install from wheels only, so a package without a wheel cannot be installed this way;
- Ubuntu packages, which cannot be installed while the session runs: naming them records the need for the base image.

```json
{"purpose": "Lattice reduction for the attack estimate", "capability": "fpylll"}
```

```json
{"purpose": "PARI's class group routines", "conda": ["pari>=2.15"]}
```

The result gives the outcome (already_satisfied, completed, awaiting_permission, denied, failed or needs_recovery), the environment's revisions before and after, the package changes, the probe results and what to do next. `provision_status` follows an operation by its ID, waiting for it to end, cancels it, or lists the environment's queue. An environment runs one operation at a time, and one that waits for permission holds up those behind it. The upgrade action updates named Conda packages already recorded; there is no uninstall and no upgrade of everything. Software under the GNU Affero GPL is refused unless the project's policy names it as an exception.

## The package cache

Sandboxes get packages only through the project's package cache. It fetches from conda-forge, PyPI and a few other registries on demand, and the policy decides each file. The common package managers in the sandbox (pip, uv, conda, mamba, cargo, go, npm) are already set up to use it, and the registries cannot be reached directly. An install from a git URL is an ordinary download, which the network rules decide, not a package install.

## Network hosts

The project's policy decides which hosts code in the sandbox can reach. The first connection to a site the session has not used can fail with "Permission denied" while the platform admits the site, which takes up to half a minute; a later result's platform notices say whether it was admitted or refused. `admit_hosts` avoids the failed first connection: name the hosts (host or host:port, port 443 by default, at most 32 a call) before code contacts them, and each comes back admitted, or refused with the reason.

Admitting a host the policy does not name already reloads the sandbox's network rules, which cuts off transfers in progress, so admit every host a script needs before starting it. A reachable host still has each request decided by the policy. Connector tools run on their servers, so they need no admission.

## Refusals and approvals

Two kinds of answer look alike but mean different things.

- A request that needs approval fails with `approval_required` until someone approves it; a provisioning operation in that state is awaiting_permission. Say what awaits approval, go on with work that does not need it, and retry once it is approved. For provisioning, `provision_status` with the operation's ID picks it up after the user has answered.
- Any other refusal with a reason, such as `policy_denied`, a refused host or a denied package, is the user's policy deciding, not an obstacle. Do not resubmit it unchanged, or reach the same content another way: another host, a mirror, another package source, another tool or an encoding. Say what was refused and why you needed it, use what is allowed, or ask the user with `ask_user` where it is offered.

A source you could not reach is unavailable, not evidence that what it holds does not exist.

## When something is missing

If a capability the work needs cannot be installed, say so, and say what it was for. Substituting a different tool without saying so changes what a result means. If an install ends needs_recovery, the environment refuses new executions until an operator recovers it: tell the user.
