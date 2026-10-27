<!-- SPDX-License-Identifier: MIT -->
# Babbage lab: the workspace answers network and package requests (A4, D97)

Run on babbage on 2026-10-27, from 16:19 to 16:28 UTC, against OpenShell 0.1.5 (`~/.local/opt/openshell-0.1.5/bin`), from a fixed detached worktree of `k8s-sync` at `db557d97`, with `sandbox-science/minimal:48d206ba` built locally from its committed inputs. The fixture is `.agents/labs/a4_approvals.py` ([README](../../labs/README.md#workspace-answers-to-network-and-package-requests-a4-d97)), copied into that worktree for the run. It is the open part of backlog A4: research code's requests escalate, reach the workspace as cards, the user's answers go back through the worker, the middleware or the provisioning path acts on them, and later requests are covered by the grant or refused after a deny. No model was called; teardown was complete.

**In short:** every answer kind did what D97 says, on a real gateway, through the workspace's own HTTP API (login token, session cookie, same-origin `POST`s). One small product defect was found and fixed (the cards' scope menu); two fixture bugs were fixed during the run.

## Setup

- A disposable root `/var/tmp/ss-a4lab-f0d8496f` from `make_root`, with its own gateway (VM driver, mTLS), policy middleware, package cache, worker (a run-service client and an operator client) and workspace server (`sandbox-science-workspace serve` with `--worker`, as the launcher starts it), on random loopback ports, as `systemd-run --user` units.
- Project `a4lab`, profile `minimal`: sessions s1 and s2 in the default environment, s3 in an environment of its own. The four hosts were admitted ahead of use.
- **The policy:** `examples/policy.yaml` without its MCP server and credential; GETs to example.com, example.org and www.example.com escalated by an exact-host rule; `packages.pypi` escalated. POSTs escalate by `external-writes`.
- **Requests** were driven without a model: research code by shell executions through the worker (Python's urllib, and `pip download`), and provisioning operations by `POST /v1/provisioning`, as the `provision` tool does.

## Results

| Answer | Request | Card | Effect | Later request |
| --- | --- | --- | --- | --- |
| Once | GET example.com/ (code) | network, scopes once and project | retry 200 | identical 200; another path asked again (new request) |
| Once | `pip download six` (code, package cache) | packages, once and project | retry downloads | — |
| Session | s1 provisioning of idna (PyPI file set) | packages, once, session and project; s1 only | operation completed | s1's next operation (iniconfig) answered by the relay, completed; s2's operation was not answered by it (still waiting after 10 s) |
| Project | GET example.org/ (code) | network, once and project; `session` refused 403 | retry 200 | other path 200; the s3 sandbox's first try was refused, the relay answered it in about 4 s, then 200 |
| Project | s2 provisioning of attrs | packages | operation completed | research code's `pip download tomli` refused once, then answered by the relay and downloaded |
| Deny | GET www.example.com (code) | network | retry `approval_denied` | identical later `approval_denied` |
| Deny | POST example.net with a JSON body (code) | network, max grant once, scopes once only; method, host, path, `application/json`, 368 bytes, four field names | `project` and `session` refused 403; retry `approval_denied` | — |
| Deny | s2 provisioning of packaging | packages | operation `denied` (`permission_denied`, stage `permission`) | — |

- **Where cards show:** a network card named no session and was listed in every session; a provisioning card only in its own session.
- **Grants panel data** (`GET …/grants`): a project host grant for example.org's reads (2 uses), a session grant of pypi for s1 (2 uses) and a project grant of pypi (2 uses), each with its last use. Revoking the session grant removed it from the listing.
- **Screenshots** in the built-in browser pane, logged in with the lab's printed token: the network and package cards in s2, each card's scope menu, and the Grants panel.

## Defects

- **Fixed (`fix(workspace)`):** a card's scope menu stayed open when another card's caret was pressed, since the outside-press check matched any card's `.allow-split`, and Escape did not close it. It now checks its own split button and closes on Escape; `chatPermission.dom.test.ts` covers both.
- **Fixture bugs:** the first fetch script was mis-quoted (fixed: the code goes base64-encoded), and the fetch sent urllib's default User-Agent, which example.org refuses with its own 403. The project-grant retries in `answers.json` therefore show 403 without a policy reason; the middleware log shows them allowed, and `project-recheck.json`, with the fixed fetch, shows 200 in s1 and s3.

## Not exercised

- A conda channel as the escalated source (PyPI only), git requests, and expiry (`hours`) of a grant.
- Revocation's effect on the middleware: the relay's host answer lasts an hour, so a revoked host grant was only checked in the listing.
- Re-authentication for approvals (D54) and the direct path's MCP cards (covered by the A2 lab).

## Evidence

Outside Git, in `/var/tmp/ss-a4lab-evidence-f0d8496f/`: `ready`, `cards`, `answers`, `project-recheck`, `relay-timing` and `cleanup` receipts (status and reason codes, request numbers, scopes, card shapes, grant rows; no bodies or tokens), four screenshots, and `SHA256.json` (SHA-256 `bfa549e1c7a2c9fd8fbc7a44da18addb1f48068acedfaed82300541623f3c346`). The fixture's own teardown deleted both sandboxes, stopped the five units, checked ports, processes and the driver directory, and removed the root.
