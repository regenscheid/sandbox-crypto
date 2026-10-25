# Handoff

Updated 2026-10-25. Read [STATUS](STATUS.md), [CONVENTIONS](CONVENTIONS.md), [decisions](../docs/DECISIONS.md) D95-D103 and D92.

- **Branch:** `k8s-sync`, in its own worktree, pushed to `origin/k8s-sync`; no PR. Keep the primary checkout and the personal instance as they are.
- **Checks:** the list in [CONVENTIONS](CONVENTIONS.md#language-and-tooling), including `scripts/check_repo_hygiene.py` (it also checks that links resolve). One suite at a time, at most `-n 4`.

- **B9 is built** (2026-10-25, [backlog](BACKLOG.md)); its control store steps are 83 to 85 and every tool call now needs a `purpose_note`, so a scripted lab call without one is refused. **B10** (the workspace rendering) is built too (2026-10-25), apart from a per-message model choice (the Compute tab shows each kernel's current cell; memory, CPU and disk were dropped by the owner); B3's live-found defects are fixed offline (runtime manager, worker reconcile, image license check) and B7 (child environments) and A3's classification review view are proposals awaiting the owner; the test instance on the cluster still runs the images from before B9 and B10, so a republish and redeploy is what shows them live.

## Next, in order ([backlog](BACKLOG.md#architecture-2026-10-23-implementation-sequence))

1. **A2, the direct MCP path (D98):** run the babbage lab of [the prototype](../docs/specs/mcp-middleware-prototype.md); then workspace answers to the middleware's MCP requests, denied-tool listings and binary results. The owner then decides whether the MCP proxy retires.
2. **A3:** the workspace's classification review view; proxy-side drift findings; a babbage lab of the compiled lockdown (D95, D96).
3. **A4:** cards for the direct path's MCP requests; a babbage lab of the workspace answering network and package requests (D97).
4. **A5:** code egress by binary stays open for the owner (OpenShell 0.1.5 cannot express it).
5. **A8:** the web usage view; a real-model lab of check-ins (D102).
6. **A10:** measure shell against kernel use in recorded sessions; steer agents to the kernel.
7. **B3 to B6:** first deployment, durability extras, telemetry rules, job sandboxes (D92, D93, D103).

## Gates

- **Babbage lab:** the middleware MCP path (A2) needs it before the proxy's fate is decided.
- **First cluster deployment (B3):** it counts as deployed after three live proofs: egress only through OpenShell; Kata in effect with tier 3 recorded; the manager cycle ([proofs](../docs/KUBERNETES.md#first-deployment-scope-and-proofs)). The operator does the cluster preparation ([checklist](../docs/KUBERNETES-PREP.md)).

## Standing rules

- Agents do not push `dev` or `main`. Images stay private on ghcr.
- The personal babbage instance is untouched; labs stay off `ss-personal-*`.
- Coding goes to cheaper models; security, migrations and concurrency get an independent review.

## Workflow and protections

Use signed commits, no attribution lines; do not push or mutate preserved services implicitly. Independent read-only review applies to security, concurrency, storage and new features. Current tool/developer instructions govern agent spawning.

Run one Python suite at a time, at most `-n 4`, with `SS_LIVE_GATEWAY` and `SS_INTEGRATION` unset and a unique `/var/tmp/sandbox-science-test-cache-*` basetemp. `/tmp` and `/home/andy` are Git checkouts. Use the locked `.venv`; strict checks use `pyright --pythonpath .venv/bin/python`. Regenerate API types after contract changes. Logs/screenshots/raw evidence stay outside Git; reports contain only sanitized summaries and hashes.

Never launch a real service from an actively edited checkout. Never read/list/copy/print real keys under `~/.config/sandbox-science/secrets/`; authorized programs receive an existing path only. No sudo, security-setting changes, Docker pruning or destructive actions on real data. SNOVA and its archive stay preserved; do not recreate its stopped monitor. Product prompts are independently written; do not import proprietary-derived OpenScience skills.
