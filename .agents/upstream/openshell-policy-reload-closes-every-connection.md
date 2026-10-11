# OpenShell: loading a policy that only adds a rule closes every open connection

**Status.** Kept here, not filed upstream: filing is the user's call. This is a feature request rather than a bug: the behaviour is documented. Seen on babbage in lab 10 (2026-10-11, [report](../docs/agent-reports/babbage-openshell-lab-10-2026-10-11.md)).

**Affects.** OpenShell v0.1.3 (tag commit `e1f3c82caa3e`). Paths below are relative to the OpenShell repository at that tag.

## Summary

When a sandbox loads a new policy generation, OpenShell closes every connection opened under the previous one: keep-alive connections, tunnels, WebSockets and streams (`docs/how-it-works/policies/manage-policies.mdx`, "Network rules"). Relays wait for the generation to go stale and then close (`crates/openshell-supervisor-network/src/opa.rs`, `wait_until_stale`). This happens even when the new generation only adds a rule, so that every connection the old generation allowed is still allowed.

Approving a draft proposal is such a change: it adds one rule. So is any other workflow that admits hosts as they are needed. Each approval interrupts every transfer in progress in the sandbox.

## Steps to reproduce

1. In a VM sandbox, stream a slow chunked response from an allowed host: one chunk per second for 60 s.
2. While it runs, add a rule for another host and wait for the sandbox to load it (`openshell policy set --wait`).

**Expected.** The stream continues, because the new rule does not change its decision.

**Actual.** The connection is closed when the sandbox loads the new policy, after 13 to 14 chunks. The server saw `BrokenPipeError`. The client received complete chunks and then end of stream, with no terminating chunk. Python's `http.client`, reading the response line by line, reported the body as complete without an error, because its `peek` ignores an incomplete read. By CPython's source, `read()` would raise `IncompleteRead` instead; the lab did not run that.

## Impact

Workflows that admit hosts on demand reload the policy often, and each reload cuts downloads, package installs and model or dataset transfers in progress. Clients without retries fail. Clients that read chunked bodies line by line can keep a truncated file without noticing.

## Suggested fix

When a new generation is published, re-evaluate each open connection against it, and close only those whose decision changed. Their host, port, binary and, for inspected connections, middleware selection are already known. Failing that, skip the close when the new generation only adds rules or endpoints.

## Our mitigation

The worker batches all hosts it admits from one check of the drafts into one revision. The policy docs tell users that admissions interrupt transfers, and that downloads should be checked by length or checksum ([policy](../../docs/POLICY.md), "Mapping to enforcement").
