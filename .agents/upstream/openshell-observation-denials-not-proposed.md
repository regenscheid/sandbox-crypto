# OpenShell: a refused connection to an unapproved DNS name is not proposed when a host-less endpoint allows the host

**Status.** Kept here, not filed upstream: filing is the user's call. Seen on babbage in lab 10 (2026-10-11, [report](../docs/agent-reports/babbage-openshell-lab-10-2026-10-11.md)) and traced in the source.

**Affects.** OpenShell v0.1.3 (tag commit `e1f3c82caa3e`), VM driver: workloads whose lookups go through policy DNS. Paths below are relative to the OpenShell repository at that tag.

## Summary

Policy DNS answers a lookup for a name that no endpoint names with a placeholder address, and stages an observation "so the later TCP attempt can be denied with both and proposed". Whether that attempt is proposed depends on which check refuses it:
- If no endpoint covers the host, the policy engine refuses it, and the supervisor sends a denial event that becomes a draft proposal.
- If an endpoint does cover the host (a host-less endpoint with `allowed_ips`), the policy engine allows it. The connection is then refused because an observation cannot authorize egress, and that refusal only writes a log event. No draft is created, so neither the advisor's review nor automatic approval ever sees the host.

## Steps to reproduce

1. Create a VM sandbox whose policy has a host-less endpoint, for example ports 80 and 443 with `allowed_ips` of public ranges, and names no host.
2. In the sandbox, fetch `https://www.example.com/`. The lookup returns `198.18.0.x` and the connection fails with `EACCES`.
3. Wait 30 s, then run `openshell rule get <sandbox> --status pending`.

**Expected.** A pending draft for the program, `www.example.com` and port 443, as for a host no endpoint covers.

**Actual.** `No network rules for sandbox '<sandbox>'`. The sandbox log shows the refusal, but no `Flushed denial analysis to gateway` line follows:

```
NET:REFUSE [MED] DENIED www.example.com [reason:policy_dns_ineligible]
CONFIG:OBSERVATION [INFO] Policy DNS staged unapproved name www.example.com synthetic=198.18.0.2 for TCP policy review
Denied staged transparent connection destination=198.18.0.2:443 reason=unapproved DNS observation cannot authorize egress
NET:OPEN [MED] DENIED /opt/sandbox-science/bin/python3.14(0) -> www.example.com:443 [reason:transparent_tcp_observation_denied]
```

With the host-less endpoint removed, the same fetch is refused by the policy engine (`endpoint www.iana.org:443 is not allowed by any policy`), and a draft appeared 2.8 s later.

## Where it happens

In `crates/openshell-supervisor-network/src/proxy.rs`, for a staged transparent connection:
1. When the policy engine denies the connection, the supervisor calls `emit_denial_simple(...)` with stage `transparent_tcp_connect`, which feeds the denial aggregator (around line 735).
2. When it allows the connection but `pinned_transparent_plan` fails because the record is an observation, the supervisor only calls `emit_staged_transparent_denial(...)`, the log event (around lines 763-781). No denial event is sent.

## Impact

A policy can have a host-less endpoint and still need hosts reviewed. In VM sandboxes such an endpoint admits only connections to addresses, since names resolve only when an endpoint names them. The hosts that need review are therefore exactly the ones that are never proposed.

## Suggested fix

Send a denial event on the observation branch too, with the mapped host, the port and the binary, so that the aggregator drafts a proposal. The draft's prover check and the reviewer can then decide whether to name the host.

## Our mitigation

Since lab 10, the Sandbox Science compiler emits no host-less endpoint. Hosts the policy names get endpoints, and the worker admits every other host on demand from the drafts OpenShell creates (D69).
