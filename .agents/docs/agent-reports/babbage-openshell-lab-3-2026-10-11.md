# OpenShell v0.1.3 lab 3 on babbage, 2026-10-11

Runtime validation of the compiled provider-credential bindings (PR #13, D57 amended) on a real gateway, followed by a full teardown. Like the [first](babbage-openshell-lab-2026-10-10.md) and [second](babbage-openshell-lab-2-2026-10-10.md) labs, this report records observed behaviour.

## Setup

- **Gateway, policy middleware, package cache.** As in lab 2, on loopback in transient systemd user units, with all state in one lab directory. Sandboxes had 2 vCPUs, 2 GiB of memory and a 4 GiB overlay disk.
- **Policy.** `examples/policy.yaml`, compiled for one host (the cache at `host.openshell.internal:8090`, the MCP proxy at `host.openshell.internal:8080`). It binds provider `github-readonly` to `api.github.com` and `github.com`. The compiled policy therefore has:
  - credential endpoints for both hosts on port 443, with path `/**`;
  - an egress endpoint for `github.com` on port 80 only, because the source-download and git-fetch rules also name it.

  The policy was stored in the middleware before the sandbox was created, and the sandbox was bound to it afterwards.
- **Provider.** `examples/openshell/profiles/github-token.yaml` (no endpoints) passed `openshell provider profile lint` and was imported. Provider `github-readonly` was created from a synthetic token, `test-secret-sandbox-science-lab3`, passed through the environment.
- **Sandbox.** `docker.io/library/buildpack-deps:noble-scm`, created with `--provider github-readonly`.
- **Echo server.** On `127.0.0.1:8080`, the MCP proxy's port. It recorded the headers it received on the host and never returned them.

## Results

| Check | Result |
| --- | --- |
| The compiled policy with the provider attached | Accepted: no endpoint ambiguity error. The supervisor reported `CONFIG:LOADED` |
| The sandbox's environment | `GITHUB_TOKEN` held only a placeholder. `GH_TOKEN` was unset: OpenShell places only the key the provider stores, not every name a profile lists. The token appeared nowhere |
| `api.github.com` | No header: 401 "Requires authentication". The placeholder: 401 "Bad credentials". OpenShell forwarded it rather than refusing it with `credential_endpoint_mismatch`, so the credential endpoint was selected |
| `github.com` over HTTPS | The placeholder on a smart-HTTP request was forwarded, and GitHub answered 401. `git ls-remote` of a public repository worked |
| `github.com` on port 80 | Admitted by the egress endpoint: 301 to HTTPS |
| An unbound destination | The placeholder sent to the echo endpoint: 403 `credential_endpoint_mismatch`. Nothing reached the server |
| The middleware decides first | A POST to `api.github.com` with the placeholder: 403 `approval_required` under `sandbox-science-credentials`, recorded with `on_timeout: deny` |
| Plain HTTP to port 443 | `curl http://api.github.com:443/user` and `curl http://github.com:443/` with the placeholder: curl got an empty reply for both. OpenShell's log, which still held the `github.com` request, shows it allowed by the credential endpoint's L7 rules and then by the middleware, with no credential error. See finding 1 |
| Traces of the token | None in the middleware's, the cache's or the gateway's logs, in the middleware's or the cache's state, or in the sandbox log |

## Findings

1. **OpenShell sends a substituted credential over plain HTTP to port 443. Seen at runtime in a VM sandbox.**
   - OpenShell logged the plain-HTTP request as `http://github.com:443/` and allowed it twice: by the credential endpoint's L7 rules, then by the middleware.
   - It logged no credential failure. A failed substitution returns 403 `credential_endpoint_mismatch` or 500 `credential_unavailable`, so the placeholder was substituted and the request was written upstream. GitHub's TLS port then closed the connection.
   - The middleware allowed the request, so OpenShell told it the scheme was `https`. Had it been told `http`, the `credential_unencrypted` check would have refused the request.
   - Lab 2 had already seen OpenShell substitute a credential into plain HTTP, on a bound loopback endpoint.
   - The [bug description](../../upstream/openshell-credential-substitution-over-plain-http.md) records this. It remains a residual risk ([policy](../../../docs/POLICY.md), "Residual risks").
2. **The compiled bindings work as designed.** Three things lab 2 had seen only in a hand-written policy now held for the compiled policy:
   - a bound host that the rules also name: `github.com`, with its egress endpoint on port 80 and its credential endpoint on 443;
   - an endpointless profile;
   - a binding to two hosts.

## Cleanup

- **Removed:** the sandbox, the provider, the units, including the echo server, and the lab directory (681 MB).
- **Checked afterwards:**
  - no OpenShell, middleware, cache or echo processes;
  - ports 17670, 50051, 8090 and 8080 free;
  - no OpenShell paths under `~/.config`, `~/.local/state` or `~/.local/share`;
  - no driver socket directories in `/tmp`.
- **Kept:** the user journal entries of the transient units, and the installed OpenShell binaries.
