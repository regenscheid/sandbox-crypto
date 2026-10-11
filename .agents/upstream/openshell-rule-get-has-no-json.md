# OpenShell: `openshell rule get` has no machine-readable output

**Status.** Kept here, not filed upstream: filing is the user's call. This is a feature request. Seen while building admission on demand (D69), and checked in lab 10 (2026-10-11, [report](../docs/agent-reports/babbage-openshell-lab-10-2026-10-11.md)).

**Affects.** OpenShell v0.1.3 (tag commit `e1f3c82caa3e`), `crates/openshell-cli/src/run.rs`, `sandbox_draft_get`.

## Summary

`openshell sandbox get`, `policy get`, `provider list` and `profile list` take `-o json`, but `openshell rule get` prints text only. A program that answers draft proposals must therefore parse that text:
- the host and port from the `Endpoints:` line, which joins endpoints and their tags with `, `;
- the chunk ID from `Chunk:`.

The text also prints fields a sandbox controls, such as the program path. A path can hold newlines and so forge whole blocks. The output never shows a rejected proposal's reason.

## Suggested change

- Add `-o json` (and `yaml`) to `rule get` and `rule history`. Print each chunk as the `PolicyChunk` message the gateway returns, including `host`, `port`, `binary`, `proposed_rule`, `rejection_reason` and `review_token`.
- In the text output, escape control characters in fields that come from the sandbox.

## Our mitigation

The worker parses the text defensively: chunk IDs are validated, at most 32 drafts and 64 endpoints are handled per check, and text from the sandbox is made printable. Every host it reads is decided by the policy library, so a forged block can only add hosts the sandbox could have tried anyway ([worker](../../docs/WORKER.md#admission-on-demand)).
