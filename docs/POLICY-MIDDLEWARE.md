# Policy middleware

The policy middleware decides every HTTP and git request a research sandbox makes (D69, D70). OpenShell's supervisor proxy carries the traffic, terminates TLS, checks addresses, injects provider credentials and logs. It calls the middleware for each request its network rules admit, before it injects credentials, and the middleware applies the [policy library](POLICY.md) to the request. The code is in `packages/policy-middleware` (`sandbox_science.policy_middleware`).

It targets OpenShell v0.1.3. The OpenShell contracts it implements are vendored unmodified in `packages/policy-middleware/proto` (Apache-2.0).

## How a request is decided

1. OpenShell admits the request with the compiled sandbox policy (admission endpoints, see [policy](POLICY.md), "Mapping to enforcement") and calls `EvaluateHttpRequest`. The call carries the request's method and target, its headers, its body (de-chunked but not decompressed, at most the registered `max_payload_bytes`, itself at most 4 MiB) and the middleware entry's `config`, which names the project policy revision: `{policy: {name, digest}}`.
   - The target's host is the destination the client asked the proxy for, a name or an IP literal, lowercased; OpenShell has already checked that the `Host` header matches it. The path is OpenShell's canonical form and the query is raw.
   - OpenShell withholds `Authorization`, `Proxy-Authorization`, `Cookie`, `Host`, `Content-Length`, `Transfer-Encoding`, the hop-by-hop headers (including `Upgrade` and `Connection`), `Expect`, `X-Amz-*` and `X-OpenShell-Credential*`. Other headers arrive in wire order with lowercased names, including credential placeholders in custom headers.
2. The middleware checks that the call comes from the supervisor of the sandbox the request names (see [Authentication](#authentication)).
3. It looks up the revision the sandbox is bound to (see [Policy store](#policy-store)). The revision in `config` must be that revision, so a sandbox that still runs a compiled policy from an older revision is denied until OpenShell applies the new one.
4. It rebuilds the request for the library: the URL from scheme, host, port, path and query; `Content-Type` (more than one is ambiguous); whether the body is non-empty; and whether the request is a WebSocket upgrade (`Sec-WebSocket-*` headers, since OpenShell withholds `Upgrade`). OpenShell labels the scheme by how the request reached its proxy, not by whether it used TLS: everything through a CONNECT tunnel is `https`, even plain HTTP to port 80. Rules without ports match the scheme's default port, so on ports 80 and 443 the port decides the scheme; a deny rule without ports therefore covers both, however the request arrived.
5. The library classifies and evaluates it under the bound policy, the pool ceiling and the hard floor.
   Requests to the package cache are the exception. The compiler names the cache's host and port in `config.package_cache`, and the middleware classifies requests to it from the cache's URL layout instead. They become package operations, decided by the policy's `packages` section ([package cache](PACKAGE-CACHE.md)). Anything but a GET or HEAD of a path in that layout is refused. Headers do not change the decision: a range request (cabal updates Hackage's index with one) is decided by its path like any other GET. An allowed request to the cache gets the header `X-Sandbox-Science-Sandbox` with the sandbox's ID, written over any the sandbox sent, so the cache can give each sandbox a budget of upstream fetches ([package cache](PACKAGE-CACHE.md#logging-and-limits)).
6. A git fetch (`POST …/git-upload-pack`) that the policy does not deny must carry a well-formed upload-pack request body, so a fetch rule cannot carry arbitrary payloads to a git host. The check (`gitproto.py`) follows git's protocol documents and what git, libgit2, dulwich and go-git actually send, in both protocol versions, and is tested against request bodies captured from git 2.53. It accepts only git's keywords and capabilities, lowercase object IDs and bounded values, and nothing after the end of the request. Gzip bodies are inflated with a bound and must be a single, plausibly sized gzip member. OpenShell never passes a truncated body: a body over the limit fails the middleware instead. The check does not bound volume, since object IDs and values such as `ref-prefix` can still carry most of a body ([policy](POLICY.md), "Residual risks").

The middleware runs before OpenShell injects credentials and cannot tell which requests will receive one. OpenShell substitutes a provider's credential only at the hosts the policy binds it to (`credentials.providers[].hosts`), and refuses requests that carry its placeholder anywhere else. A request to one of those hosts, on any port, therefore counts as credentialed: its escalations are denied on timeout rather than proceeding as alerts, unless the rule sets `allow_credentialed: true` ([policy](POLICY.md), "Escalation timeout"). Requests to other hosts cannot carry a credential.

## Results

| Library decision | OpenShell result | Reason code | Record |
| --- | --- | --- | --- |
| `allow` | allow | | |
| `alert` | allow, with a finding | | `alert` (if it cannot be recorded, the request is denied with `audit_unavailable`) |
| `escalate` | deny, until answered ([permission requests](#permission-requests)) | `approval_required`, `approval_denied` or `escalation_timed_out` | `escalation` on the first occurrence: a permission request, which the reason names. If it cannot be recorded, there is no request to approve, so the request is denied with `audit_unavailable` instead |
| `deny` by a rule or the default | deny | `policy_denied` | |
| `deny` by the pool ceiling | deny | `pool_ceiling_denied` | |
| `deny` by the hard floor | deny | `destination_forbidden` | |
| unclassifiable request | deny | `request_unclassifiable` | |

The middleware itself can also deny: `git_request_invalid` (a fetch body that is not an upload-pack request), `credential_unencrypted` (plain HTTP to port 443 of a host a credential is bound to, which OpenShell would send unencrypted), `sandbox_unbound`, `policy_stale`, `policy_unavailable` and `config_invalid`. OpenShell returns the reason code to the sandbox in its `middleware_denied` error and logs it; the free-form reason stays in the middleware's own log. Calls it cannot authenticate fail with a gRPC error, which OpenShell treats as a middleware failure and, with `on_error: fail_closed`, blocks.

**WebSockets.** The library does not support upgrades, since a WebSocket would carry traffic the policy never sees. OpenShell first asks about the upgrade request, which the middleware denies as unclassifiable because of its `Sec-WebSocket-*` headers. As a second check, the service also declares the `WEBSOCKET_MESSAGE` binding and denies every upgrade at its preflight (`websocket_not_supported`), before OpenShell contacts the server.

**Monitors.** The middleware counts the bytes each sandbox sends in allowed requests (request line, visible headers and body) over a sliding hour and reports a monitor as a finding and a `monitor` record, at most once an hour per sandbox. Response bytes are not counted. Monitors never change a decision.

**Records.** Escalations, alerts and monitor findings go to the [event store](EVENT-STORE.md), whose hash chain makes later changes evident: `events.db` in the state directory, unless the settings name another file (`event_store`). Each record the middleware writes has the kind `escalation`, `approved`, `alert` or `monitor`, the source `policy-middleware`, the store's time, and a payload with the sandbox, request id, policy revision, rule and reason. Escalations and alerts add the request's method, scheme, host, port and path, and only the size of its query string, which can hold data and credential placeholders; bodies are never recorded. An append returns once its record is on disk, which on babbage adds about 2 ms (median; p99 about 8 ms) to a request that is escalated or alerted, and nothing to the others. It waits at most 250 ms for the store, so keep the registration's `timeout` above that. An alert whose record cannot be stored is denied (`audit_unavailable`); an escalation is denied anyway, and a monitor finding is still returned.

## Permission requests

An escalated request becomes a permission request (D76). The logic is shared with the MCP proxy, in `sandbox_science.policy.approvals`; its keys come in two kinds, `http` here and `mcp` in the proxy ([MCP connectors](MCP-CONNECTORS.md)).
- **Recording.** Its first occurrence is recorded as an `escalation`, whose sequence number identifies the request; the denial's reason names it ("permission request 12"). The record's `target` is what an answer covers: the sandbox, policy revision, method, host, port and path. Retries of the same request share its fingerprint and are not recorded again, so a sandbox retrying a denied write does not grow the store.
- **Answering.** An operator answers on the pool host, until the GUI does it with re-authentication (D54):

  ```bash
  uv run sandbox-science-policy-middleware pending --settings middleware.toml
  uv run sandbox-science-policy-middleware approve --settings middleware.toml --request 12 [--scope host] [--for 8]
  uv run sandbox-science-policy-middleware deny --settings middleware.toml --request 12
  ```

  The answer is an `approval` or `denial` record from the source `operator`, in the same hash-chained store.
- **Approvals.** An approval covers the request itself, or with `--scope host` every path on that host and port with that method. It lasts `--for` hours (8 by default, 720 at most). Matching retries are allowed with a `policy_approval` finding and an `approved` record; if that record cannot be written, they are denied with `audit_unavailable`. A denial refuses them with `approval_denied`. The newest answer decides.
- **Scope.** Answers apply only to the sandbox and policy revision of the request, so another sandbox, or a new revision, asks again.
- **Timeouts.** A request nobody answers within its rule's escalation timeout follows `on_timeout` ([policy](POLICY.md), "Escalation timeout"). A read whose rule allows it proceeds as an alert, recorded with `timed_out_request`; anything else is denied with `escalation_timed_out`. The middleware decides this when the request is retried; it does not hold requests open.

**opam archives.** opam fetches archives from the package cache by digest, so the request names no package. The payload of an escalated or alerted archive's record therefore has `opam_archive`: the digest (`"md5:ca77…"`) and, when `package_cache_url` is set, the packages that the cache's copy of the opam index lists for it (`"packages": ["ocamlfind.1.9.8"]`, at most 100, with `more_packages` counting the rest). The middleware asks the cache's `GET /-/opam/archives/<algorithm>/<hex>` ([package cache](PACKAGE-CACHE.md), "URL layout"), waiting at most 50 ms for each step, so that the lookup and the record's append fit the 500 ms OpenShell gives an evaluation. When the cache cannot answer, the record keeps only the digest. An empty list means that no opam file in the index lists the digest. The answer never changes a decision. Such an escalation holds an install back only if the network rules also stop opam's own downloads ([policy](POLICY.md), "Packages").

## Policy store

The worker service writes, and the middleware reads, a state directory ([worker](WORKER.md), "Environments"; D73). The code is the policy library's (`sandbox_science.policy.store`), which the MCP proxy uses too, with project bindings (`projects/<project>.json`) in place of sandbox bindings:

| Path | Contents |
| --- | --- |
| `policies/<hex>.yaml` | A project policy file, named by the hex SHA-256 of its bytes; a file whose bytes do not match its name is rejected |
| `sandboxes/<sandbox id>.json` | `{"policy": "sha256:<hex>"}`: the revision an OpenShell sandbox is decided under |
| `events.db` | Written by the middleware: its records, in the [event store](EVENT-STORE.md) (with `events.db-wal` and `events.db-shm` while it is open) |

`ValidateConfig` accepts a compiled sandbox policy only when its project policy is already in the store, so the gateway refuses a sandbox policy the middleware could not decide. The gateway asks before it creates the sandbox, so the policy must be stored first, and the sandbox bound once OpenShell has assigned its id. The worker does both for the sandboxes it creates (`sandbox-science-worker policy add` stores a revision). For sandboxes made by hand, the middleware's command line does them:

```bash
uv run sandbox-science-policy-middleware store --state-dir STATE --policy policy.yaml
uv run sandbox-science-policy-middleware bind --state-dir STATE --sandbox SANDBOX_ID --policy policy.yaml
```

## Authentication

When the gateway signs extension tokens (`[openshell.gateway.gateway_jwt]`), every call carries an EdDSA JWT in its `authorization` metadata. The middleware pins the algorithm, the token type `openshell-ext+jwt`, the issuer `openshell-gateway:<gateway_id>`, the exact audience (by default `urn:openshell:extension:middleware:<registration>`, also returned as `expected_audience` from `Describe`) and the expiry, with a small leeway. Token ids repeat by design and are not checked. The gateway calls `Describe` at startup and `ValidateConfig` when it accepts a sandbox policy. Each supervisor calls `Describe` and the evaluations with a token that names its sandbox, and a request evaluation must come from the supervisor of the sandbox it names. OpenShell sends tokens only over TLS and checks the service's certificate against the registration's host name, so token verification requires the service's own certificate with that name.

For local development, a registration with `allow_insecure_transport = true` sends no tokens; the middleware then needs `allow_unauthenticated = true` and accepts any caller that can reach it.

`Describe` also negotiates the extension protocol as OpenShell's own extensions do: protocol major version 1, with the capability `openshell.supervisor-middleware.contract` supported and required on both sides.

## Running it

Settings are a TOML file (relative paths resolve against it):

```toml
registration = "sandbox-science-policy"   # the gateway registration's name
listen = "127.0.0.1:50051"
state_dir = "state"
ceiling = "pool-ceiling.yaml"
admin_hosts = ["gateway.sandbox-science.internal"]   # added to the hard floor
package_cache_url = "http://127.0.0.1:8090"          # optional: the cache from this host

[tls]
certificate = "tls/middleware.pem"
private_key = "tls/middleware-key.pem"

[tokens]
gateway_id = "openshell"
public_key = "/path/to/gateway/jwt/public.pem"
```

```bash
uv run sandbox-science-policy-middleware serve --settings middleware.toml
```

Register it with the gateway. The gateway checks it at startup, so start the middleware first. The registration's `max_payload_bytes` must not exceed the middleware's own:

```toml
[[openshell.supervisor.middleware]]
name = "sandbox-science-policy"
grpc_endpoint = "https://127.0.0.1:50051"
tls_ca_cert_path = "/path/to/middleware-ca.pem"
max_payload_bytes = 4194304
timeout = "500ms"
```

The gateway and every sandbox supervisor call the middleware at the registered address, without rewriting it.
- **VM driver (babbage).** OpenShell v0.1.3 runs each sandbox's supervisor as a process on the gateway's host; only the workload runs in the microVM. Both callers are local, so the middleware listens on loopback, as above.
- **Other drivers.** Use an address both the gateway and the supervisors can reach. Never listen on all interfaces of a host whose network is not trusted.
- **Certificates.** The service's certificate must name the registered host: for the address above, `IP:127.0.0.1`. `tls_ca_cert_path` must hold certificates only, the CA that issued it rather than a self-signed server certificate.

The compiler attaches it to every host in each research sandbox's policy (`examples/openshell/research-sandbox.yaml`).

OpenShell's client pings the service every 10 seconds, even between calls; the server allows this, since gRPC's defaults would answer with `too_many_pings` and the reconnects would fail requests closed.

## Checked on babbage

On 2026-10-10 the service ran against a real OpenShell v0.1.3 gateway with the VM driver ([lab report](../.agents/docs/agent-reports/babbage-openshell-lab-2026-10-10.md)):
- The gateway negotiated the service and verified its certificate for `127.0.0.1`, and the service verified the gateway's and the supervisors' tokens.
- Reads were allowed. Writes were denied with `approval_required` and recorded.
- Plain HTTP on port 80, which OpenShell labels `https`, was decided as `http`.
- WebSocket upgrades were denied before reaching the server.
- A real `git clone` passed the upload-pack check.
- A 5 MiB body and a stopped middleware both failed closed.
- The service adds a median of 0.7 ms per request; a 4 MiB body takes about 8 ms.

The lab also showed that a sandbox resolves only the host names its policy names. [Policy](POLICY.md), "Mapping to enforcement", describes what the compiler does about it.
