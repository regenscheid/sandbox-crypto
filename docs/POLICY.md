# Project policy file

Date: 2026-10-10. Proposed (D62). The user wants permissions to stay policy-based and to be defined in a YAML file that is quite permissive by default, with a configurable timeout after which unanswered requests fall back to deny. This document defines the proposed semantics. The illustrative file is [examples/policy.yaml](../examples/policy.yaml); its schema is not yet validated.

## Layers and precedence

Three layers combine for every operation. A lower layer can only narrow what the layer above allows.

| Layer | Owner | Editable by | Purpose |
| --- | --- | --- | --- |
| 1. Hard floor | Platform code | Nobody at runtime | Rules that keep the sandbox a sandbox, whatever the policy says |
| 2. Pool ceiling (D49) | Each worker pool's local configuration | Local user action only | Maximum resources, destinations, mounts and data classes for that pool |
| 3. Project policy (this file) | The project | The user, through the GUI or file, with revision history | Day-to-day research permissions |

**Hard floor (always deny):**

- reaching gateway, monitor, policy or engine administration APIs;
- cloud metadata endpoints and link-local addresses;
- raw secret material (credentials exist in sandboxes only as provider placeholders);
- writes to policy, monitor configuration or the event log;
- host-path mounts beyond declared volumes;
- privilege escalation, container-engine sockets, kernel module loading;
- running research code in the orchestrator.

Agents can never change any layer. They can *propose* a project-policy change through the OpenShell policy advisor path (auto-approval off), and that proposal is an ordinary escalation.

## Actions

| Action | Effect |
| --- | --- |
| `allow` | Proceed; logged |
| `alert` | Proceed; record a finding and notify |
| `escalate` | Hold only the affected operation; create a durable permission request; resume or safely retry on approval, block on denial or timeout |
| `deny` | Block; record the rule and reason |

Within the project policy, when several rules match one operation, **the most restrictive action wins** (`deny` > `escalate` > `alert` > `allow`). Broad `allow` rules therefore stay safe to write, because any narrower `escalate` or `deny` rule still applies. Operations that match no rule take `defaults.action`.

## Escalation timeout

`escalation.timeout` sets how long a request waits for an answer. When it expires, `escalation.on_timeout` applies; the default and recommended value is `deny`. A rule can override the timeout for itself. `on_timeout: allow` is accepted only for rules whose action would otherwise be `alert`, never for writes or new credential use. Other work continues while a request waits (`hold_scope: operation`). A denied or timed-out request is not retried automatically.

## What the permissive default allows

The shipped example is "quite allowing", as requested:

- **Freely allowed:** package installs from conda-forge and PyPI through the mirror; any read-only HTTP (GET/HEAD) with a URL length limit; git clone/fetch; read-type tools on crypto-research-mcp; writes to the project's shared data, with conflict checks.
- **Asks first:** any external write (POST/PUT/PATCH/DELETE), git push, crawling, agent and monitor tools that change state or spend credits, and anything unmatched.
- **Never:** MCP server configuration tools, plus the hard floor.
- **Alerts:** unusually large egress volume.

This deliberately relaxes the earlier review proposal (D44) that unknown destinations should never be reachable without approval. Reads are broadly allowed. The primary threat is agent overreach, which mostly shows up as side effects, and those still escalate. The remaining cost is that read requests can carry small amounts of data out in URLs; the URL limit and logging bound it.

## Mapping to enforcement

| Policy element | Enforced by |
| --- | --- |
| Hard floor | Platform code, OpenShell filesystem/process policy, network policy with no override path |
| `network.rules` with fixed hosts | OpenShell network policy endpoints with L7 method/path rules in *enforce* mode |
| Wildcard-host read rules (`hosts: ["*"]`) | To verify: whether OpenShell v0.1.3 endpoints accept wildcard hosts. If not, chain an MIT/Apache egress proxy (Envoy or mitmproxy) through OpenShell's `upstreamProxy` setting and enforce the wildcard rules there |
| `mcp.servers.*.tools` | OpenShell `protocol: mcp` endpoints with tool-name allow/deny rules; `escalate` compiles to deny-plus-pending-request |
| `mcp.servers.*.auth` | OAuth via the connector auth service, or bearer/custom headers via provider placeholders placed by the MCP proxy ([MCP connectors](MCP-CONNECTORS.md), D66, D67). Literal secrets in header values are rejected |
| `credentials.providers` | OpenShell providers bound to their endpoints (D57) |
| `escalate` | Deny in the enforcement layer, plus a durable request in the shared permission workflow; resume through a scoped decision (MONITORING.md) |
| `alert` | Allow, plus a monitor finding |
| `resources` | Runtime limits, capped by the pool ceiling; model budget through the LiteLLM virtual key (D61) |
| `packages` | Mirror allowlist and read-only rootfs (D44, D48) |

The policy compiler is part of infrastructure track P. It reports any rule it cannot enforce on the selected backend rather than silently dropping it, and records the effective policy revision with every execution.

## Open items

- Whether OpenShell expresses wildcard hosts with method restrictions, or needs the upstream proxy.
- MCP tool-name patterns depend on the actual tool names the server exposes per revision; pin the server revision in the policy.
- How GUI edits and file edits of the policy merge (proposed: the file is the source of truth, the GUI edits it, every change is a revision).
