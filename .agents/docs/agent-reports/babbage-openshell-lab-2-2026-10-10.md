# OpenShell v0.1.3 lab 2 on babbage, 2026-10-10

Runtime validation, against a real gateway, of the package cache (PR #11, D71) behind the policy middleware with real package clients in a sandbox, of provider credentials in research sandboxes, and of the MCP proxy's compiled policy. A full teardown followed. Like the [first lab](babbage-openshell-lab-2026-10-10.md), this report records observed behaviour.

## Setup

- **Gateway.** As in the first lab: OpenShell v0.1.3 with the VM driver, on `127.0.0.1:17670`, in a transient systemd user unit with all state in one lab directory. Sandboxes had 4 vCPUs, 4 GiB of memory and a 16 GiB overlay disk.
- **Package cache.** `sandbox-science-package-cache serve` on `127.0.0.1:8090` with all five ecosystems, `public_url = "http://host.openshell.internal:8090"`, as a user unit with `LimitNOFILE=65536`.
- **Policy middleware.** As in the first lab, with the cache and the MCP proxy in its configuration.
- **Policy.** `examples/policy.yaml`, compiled for one host: the cache at `host.openshell.internal:8090` and the MCP proxy at `host.openshell.internal:8080`. The policy was stored in the middleware before the sandbox was created, and the sandbox was bound to it afterwards.
- **Sandbox image.** `docker.io/library/buildpack-deps:noble-scm`, which has curl and git but no compiler and no Python. Every toolchain came from conda-forge through the cache.
- **Echo server.** For the credential tests, a small HTTP server on `127.0.0.1:8080`, the MCP proxy's port, recorded the headers it received on the host and never returned them, so the sandbox learned nothing it had not sent. The research policy admits that port as the MCP proxy, and the middleware lets worker code reach it.
- **Credentials.** Only synthetic tokens (`test-secret-sandbox-science-lab`, `test-secret-sandbox-science-lab-mcp`), passed to `openshell provider create` through the environment. The GitHub checks sent them to `api.github.com`, which rejects them.

## Results: package cache

| Check | Result |
| --- | --- |
| Index lookups | PyPI, conda, Cargo, Go and npm index files: 200 through the middleware and the cache |
| Refused by the middleware | `/-/health`, `/-/stats`, and a package path with a query string: 403 `request_unclassifiable` |
| Refused by OpenShell | `pypi.org`, `files.pythonhosted.org`, and the gateway's and the middleware's ports on `host.openshell.internal`: no connection, since no endpoint admits them |
| Bootstrapping micromamba | The micromamba 2.9.0 static binary from GitHub releases (`github.com`, then `release-assets.githubusercontent.com`, allowed by `source-downloads`), checked against its published SHA-256 |
| conda-forge toolchains | Python 3.13.16, Node 26.10.0, Go 1.27.2, Rust 1.98.1, GCC 15.3.0 and make 4.4.1: 51 packages and 544 MB in 15 s. micromamba 2.9 used sharded repodata (260 shard requests) |
| pip | cryptography 50.0.2 from wheels; pycryptodome 3.24.0 built from its source distribution by conda-forge's GCC, since the image has no compiler |
| npm | tweetnacl, `@noble/hashes` and `@noble/curves` 2.4.0 (scoped names). `npm ci` from a lockfile whose links name `registry.npmjs.org`: npm fetched them from the cache's lockfile route, including the scoped one |
| Go | `golang.org/x/crypto` v0.58.0, `filippo.io/edwards25519` v1.2.0 and `golang.org/x/sys`, verified against `sum.golang.org` through the cache. A ChaCha20-Poly1305 program built and ran |
| Cargo | 24 crates (`sha2`, `chacha20poly1305` and their dependencies), once HTTP/2 multiplexing was off (finding 1). The program built with conda-forge's linker and ran |
| uv | `uv lock` worked over plain HTTP |
| Second install | Every client again from empty local caches: micromamba in 11 s, the others in 0 to 2 s. The cache answered 131 requests from its store and fetched only uv's files upstream |
| Revalidation | After the 5-minute index lifetime, PyPI and npm index pages were revalidated upstream and reused |
| Totals | 564 requests (376 conda, 96 Cargo, 43 Go, 35 PyPI, 14 npm); 1.19 GB served; 424 fetched upstream; no errors. Four answers were 404s, all from the `go` command probing module-path prefixes |

## Results: provider credentials

OpenShell serves only the provider profiles imported into the gateway, and it starts with none. Four lab profiles passed `openshell provider profile lint` and were imported. Each GitHub variant ran in its own sandbox with the compiled research policy, bound in the middleware like any other.

| Check | Result |
| --- | --- |
| A1: a profile whose endpoint names `api.github.com:443`, as OpenShell's GitHub profile does | Sandbox creation refused: `network endpoint ambiguity validation failed`. The egress endpoint (`<any-host>:80,443`) and `_provider_gh_a1` (`api.github.com:443`) "overlap on port(s) 443 with conflicting metadata: allowed_ips=[161 ranges] vs []" |
| A2: the same endpoint, carrying the egress endpoint's 161 address ranges | Accepted. OpenShell added the profile's endpoint as a rule of its own, `_provider_gh_a2` |
| A3: a profile with no endpoints. The policy binds the credential with `credential_binding` on an `api.github.com:443` endpoint with path `/**` and the same address ranges | Accepted. A path selector makes the endpoint more specific than the host-less one, so OpenShell does not require the two to agree on the binding |
| The sandbox's environment | Only a placeholder, `LAB_GITHUB_TOKEN=openshell:resolve:env:v<revision>_LAB_GITHUB_TOKEN`. The token appeared nowhere in it |
| GitHub's answers | No header: 401 "Requires authentication". The placeholder: 401 "Bad credentials", the same answer as for a wrong literal token, so GitHub alone cannot show that the token was substituted |
| Substitution, observed | In A3 the policy also bound the echo endpoint. The echo server received `Authorization: Bearer test-secret-sandbox-science-lab` from both curl and wget, and the bare token in a custom header (`X-Lab-Token`) |
| Programs | wget, which no profile names, got the same substitution as curl: OpenShell scopes credentials to endpoints, not programs |
| Unbound destinations | In A2, the placeholder sent to the echo endpoint, in `Authorization` or in a custom header: 403 `credential_endpoint_mismatch`, and nothing reached the server. `api.github.com` on port 80: no connection, since only 443 is named |
| The middleware decides first | A POST to `api.github.com` with the placeholder: 403 `approval_required` from the middleware, under `_provider_gh_a2` in A2 and the bound endpoint in A3. Both were recorded in `events.jsonl` as pending escalations with `on_timeout: deny` |
| The floor holds under provider rules | A provider attached to the running A2 sandbox, whose profile admits `pypi.org` with the same address ranges: OpenShell admitted the request, and the middleware refused it with 403 `destination_forbidden`. The same PyPI index through the cache still answered |
| Traces of the token | None in the middleware's or the cache's logs and state, in the gateway's log, or in the sandbox log. The gateway stores credentials encrypted in its SQLite database. The key-encryption key is a file beside it (`gateway/credentials/key-encryption-key.bin`, mode 0600) |

## Results: the MCP proxy's policy

| Check | Result |
| --- | --- |
| B1: `examples/openshell/mcp-proxy.yaml` as compiled, without a provider | Refused: "credential_binding references provider 'crypto-research-mcp', but that provider is not attached to the sandbox" |
| B1, with a provider of that name attached | Its profile has no endpoints, which OpenShell requires for a provider named by `credential_binding`. The sandbox was created, and its supervisor reported the policy `CONFIG:LOADED` |
| B2: the same rules pointed at the echo server, with curl standing in for the proxy's executable | `initialize`, `notifications/initialized`, `tools/list` and an allowed tool were forwarded, each with the provider's token substituted into `Authorization` |
| Tools the fence must refuse | Tools the project escalates, tools it denies, an unlisted tool, a name differing only in case, and a name without the server's prefix: 403 `policy_denied` |
| Protocol version | A call without `MCP-Protocol-Version`: 403 `mcp_protocol_version_not_allowed`. OpenShell then assumes 2025-03-26, which the policy does not allow |
| Malformed traffic | A JSON-RPC batch and a server-to-client method (`sampling/createMessage`): 400 `invalid_mcp_request` |
| A placeholder in the request body | Forwarded unchanged: OpenShell neither substituted nor refused it |
| Prover | `openshell-prover check` cannot check either compiled policy: `unsupported_policy_shape`, because the research policy uses middleware and the MCP proxy's policy uses `protocol: mcp` |

## Findings

1. **Cargo needs `http.multiplexing = false` while the cache is plain HTTP.** With multiplexing on (the default), Cargo asks to upgrade plain-HTTP connections to HTTP/2 (`Upgrade: h2c`). OpenShell refuses that on inspected endpoints with 403 `unsupported_l7_protocol`, before the middleware sees the request. The cache's own tests missed it, because nothing sits between Cargo and the cache there. The client configuration in [package cache](../../../docs/PACKAGE-CACHE.md) now sets it, and the integration test uses the documented setting.
2. **Lockfiles written in a sandbox name the cache.**
   - npm records the cache's rewritten download links in `package-lock.json`, for example `http://host.openshell.internal:8090/npm/files/<sha512>/@noble/curves/-/curves-2.4.0.tgz`.
   - `uv.lock` records the cache's index and file URLs.
   - Such a lockfile does not work outside a sandbox, nor inside one after the cache's `public_url` changes. npm rewrites only `registry.npmjs.org` links to the configured registry.
   - `Cargo.lock` keeps the crates.io source, so it travels; so does `go.sum`.
   - Open question (backlog): hand out upstream links where a client maps them to the configured registry itself, or rewrite lockfiles when work leaves the sandbox.
3. **A provider whose profile names a public host cannot be attached to a research sandbox as it stands.** OpenShell refuses endpoints that overlap on host and port unless their address ranges agree. The compiled policy's host-less endpoint covers every host on ports 80 and 443, with public address ranges. OpenShell's own example profiles, such as GitHub's, therefore fail. Two arrangements work:
   - **The profile carries the address ranges (A2).** Every profile must repeat the compiler's 161 ranges exactly, and change whenever they change. The profile, not the project policy, then decides where the credential goes.
   - **The compiled policy binds the credential (A3).** Providers use profiles without endpoints, and the compiler writes a `credential_binding` endpoint, with path `/**`, for each host a provider's credential may reach. Every endpoint then comes from the compiler, and the floor and pool ceiling apply to credentialed hosts too. The project policy has to say which hosts each provider's credential is for. The MCP proxy's policy already works this way.

   The choice is the user's and is pending (backlog).
4. **Credentials go to endpoints, not programs.** As OpenShell documents, any process in the sandbox can use a placeholder, but only at an endpoint the credential is bound to. Elsewhere OpenShell refuses the request, so a placeholder cannot carry the token to another host. Substitution happens after the middleware has decided, so a request the middleware refuses never carries the token.
5. **The v0.1.3 prover cannot check these policies.** It reports any policy that uses a middleware or a `protocol: mcp` endpoint as unsupported, which covers every research sandbox and the MCP proxy. It cannot flag new credentialed reach for them, which the [threat model](../../../docs/THREAT-MODEL.md) (A3) and [monitoring](../../../docs/MONITORING.md) relied on. The compiler and the orchestrator have to make that check themselves.
6. **What the MCP proxy must do.** Send `MCP-Protocol-Version` on every request after `initialize`, and put the token placeholder in the `Authorization` header: OpenShell does not substitute placeholders in JSON-RPC bodies.
7. **The gateway's state directory is secret.** It holds the credential database and, beside it, the key that decrypts it.

## Cleanup

- **Removed:**
  - the sandboxes, providers and profiles;
  - the units, including the echo server;
  - the lab directory, 1.5 GB including the cache's 680 MB store.
- **Checked afterwards:**
  - no OpenShell, middleware, cache or echo processes;
  - ports 17670, 50051, 8090 and 8080 free;
  - no OpenShell paths under `~/.config`, `~/.local/state` or `~/.local/share`.

  The VM driver kept its sockets inside the lab directory this time, so `/tmp` had nothing to remove.
- **Kept:** the user journal entries of the transient units, and the installed OpenShell binaries.
