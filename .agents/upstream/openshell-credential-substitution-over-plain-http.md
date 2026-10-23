# OpenShell: provider credentials are substituted into plain HTTP and sent unencrypted

**Status (v0.1.5, 2026-10-21).** Partly fixed upstream: since v0.1.4 the tunnel path reports plain HTTP as `http` to supervisor middleware (`ctx.request_default_port = Some(80)` in `crates/openshell-supervisor-network/src/proxy.rs`), so the policy middleware's `credential_unencrypted` check covers it; substitution itself is still not gated on TLS (from the source; not yet observed in a lab).

**Status.** Kept here, not filed upstream: filing is the user's call. An adversarial review of the credential binding found it in the source on 2026-10-10, and the citations below were rechecked. It was then observed on babbage.
- **Lab 2:** OpenShell substituted a credential into plain HTTP sent to a bound loopback endpoint.
- **Lab 3:** in a VM sandbox, OpenShell allowed plain HTTP sent to `github.com:443` through its credential endpoint, logged no credential error, and forwarded it; GitHub closed the connection. The policy middleware allowed it as well, so OpenShell had told it the scheme was `https` ([lab 3 report](../docs/agent-reports/babbage-openshell-lab-3-2026-10-11.md)).

Packet capture was not available, so the clear-text bytes were not seen on the wire.

**Affects.** OpenShell v0.1.3 (tag commit `e1f3c82caa3e`). It affects every driver. In VM sandboxes all traffic reaches the supervisor transparently, as through a tunnel. Paths below are relative to the OpenShell repository at that tag.

## Summary

A provider credential bound to an endpoint is substituted into any request that matches the endpoint's host, port and path, whether or not the client's connection used TLS. A plain-HTTP request sent to a bound HTTPS port (443) is relayed upstream over the raw TCP connection, so the real credential crosses the network unencrypted. No policy setting prevents this, and a supervisor middleware cannot detect it on the tunnel path.

## Steps to reproduce

1. Create a sandbox whose policy has an inspected endpoint bound to a provider, either `credential_binding` or a provider profile endpoint:

   ```yaml
   - host: api.github.com
     ports: [443]
     path: /**
     protocol: rest
     enforcement: enforce
     access: full
     credential_binding: {provider: github-token}
   ```

2. Attach the provider, holding a synthetic token, as `GITHUB_TOKEN`.
3. In the sandbox, send the placeholder over plain HTTP to the bound port:

   ```bash
   curl -H "Authorization: Bearer $GITHUB_TOKEN" http://api.github.com:443/user
   ```

   curl sends `Host: api.github.com:443`, because 443 is not plain HTTP's default port.

**Expected.** OpenShell refuses the request, or forwards it without substituting the credential, because the connection is not encrypted.

**Actual (from the source).** OpenShell substitutes the token and writes the request to the upstream connection in clear text. GitHub's TLS port rejects it, but only after the token has crossed the network.

## Where it happens

- **Tunnel path.** `peek_tunnel_protocol` finds plain HTTP/1 rather than TLS. The request is relayed with `relay_http_stream` on the raw upstream socket (`crates/openshell-supervisor-network/src/proxy.rs:3201-3210`). Only the TLS branch sets up upstream TLS (`proxy.rs:3091-3108`).
- **Authority check.** `request_authority_matches_endpoint` compares the request's host and effective port with the endpoint's. `Host: api.github.com:443` gives port 443, so it passes (`crates/openshell-supervisor-network/src/l7/relay.rs:220-236`).
- **Credential selection.** `scoped_context_for_request` installs the resolver for the endpoint's host, port and request target. No transport check exists (`l7/relay.rs:102-129`; see also `crates/openshell-core/src/provider_credentials.rs`).
- **Forward proxy.** `http://api.github.com:443/…` through OpenShell's explicit proxy goes the same way:
  - the absolute-form URI is parsed (`proxy.rs:5339-5350`);
  - the endpoint's credentials are chosen by host, port and path (`proxy.rs:6405-6423`);
  - plain-HTTP requests then dial the destination directly (`proxy.rs:6562-6568`).

  Unlike tunnels, this path reports the scheme as `http` to middlewares.

## Why policy cannot prevent it

- **No TLS-required mode.** `NetworkTlsMode` has only `UNSPECIFIED` (auto-detect and terminate when inspection is configured) and `SKIP`. `TERMINATE` and `PASSTHROUGH` are rejected (`proto/sandbox.proto:106-115`).
- **Middlewares are told `https` on the tunnel path.** `apply_middleware_chain_with_request_id` passes the scheme `"https"` unconditionally (`l7/middleware.rs:509-527`). A middleware therefore cannot tell plain HTTP from TLS there.
- **The credential guard checks only for inspection.** It requires credentialed traffic to be inspected, not encrypted (`proxy.rs:2903`).

## Impact

- **Destination.** The binding fixes the host, port and allowed addresses, so a workload cannot redirect the credential to a server it controls.
- **Exposure.** The credential travels unencrypted from the gateway to the upstream address, where observers on that network path can read it. It is the real secret, not the placeholder.
- **How it happens.** A client misconfigured with `http://…:443` leaks it by accident. A malicious workload could leak it deliberately, to an accomplice on the path.

## Suggested fix

- Resolve placeholders only when the upstream leg is TLS. Otherwise refuse the request with `credential_endpoint_mismatch`, or a new reason code.
- Alternatively, add an endpoint option that requires TLS and refuses plain HTTP.
- Report the actual transport to supervisor middlewares instead of a fixed `https`.

## Our mitigation

Sandbox Science binds credentials to port 443 only (D57, amended). The policy middleware refuses requests that OpenShell reports as plain HTTP to a bound host's port 443, with reason code `credential_unencrypted`. That covers the forward proxy. The tunnel and transparent path remains a residual risk ([policy](../../docs/POLICY.md), "Residual risks").
