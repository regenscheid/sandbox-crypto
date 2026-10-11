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
6. A git fetch (`POST …/git-upload-pack`) that the policy does not deny must carry a well-formed upload-pack request body, so a fetch rule cannot carry arbitrary payloads to a git host. The check (`gitproto.py`) follows git's protocol documents and what git, libgit2, dulwich and go-git actually send, in both protocol versions, and is tested against request bodies captured from git 2.53. It accepts only git's keywords and capabilities, lowercase object IDs and bounded values, and nothing after the end of the request. Gzip bodies are inflated with a bound and must be a single, plausibly sized gzip member. OpenShell never passes a truncated body: a body over the limit fails the middleware instead. The check does not bound volume, since object IDs and values such as `ref-prefix` can still carry most of a body ([policy](POLICY.md), "Residual risks").

The middleware runs before OpenShell injects credentials and cannot tell which requests will receive one. Every request of a project that lists `credentials.providers` therefore counts as credentialed, so its escalations are denied on timeout rather than proceeding as alerts ([policy](POLICY.md), "Escalation timeout").

## Results

| Library decision | OpenShell result | Reason code | Record |
| --- | --- | --- | --- |
| `allow` | allow | | |
| `alert` | allow, with a finding | | `alert` (if it cannot be recorded, the request is denied with `audit_unavailable`) |
| `escalate` | deny | `approval_required` | `escalation`: a pending permission request; the agent retries after approval |
| `deny` by a rule or the default | deny | `policy_denied` | |
| `deny` by the pool ceiling | deny | `pool_ceiling_denied` | |
| `deny` by the hard floor | deny | `destination_forbidden` | |
| unclassifiable request | deny | `request_unclassifiable` | |

The middleware itself can also deny: `git_request_invalid` (a fetch body that is not an upload-pack request), `sandbox_unbound`, `policy_stale`, `policy_unavailable` and `config_invalid`. OpenShell returns the reason code to the sandbox in its `middleware_denied` error and logs it; the free-form reason stays in the middleware's own log. Calls it cannot authenticate fail with a gRPC error, which OpenShell treats as a middleware failure and, with `on_error: fail_closed`, blocks.

**WebSockets.** The library does not support upgrades, since a WebSocket would carry traffic the policy never sees. OpenShell first asks about the upgrade request, which the middleware denies as unclassifiable because of its `Sec-WebSocket-*` headers. As a second check, the service also declares the `WEBSOCKET_MESSAGE` binding and denies every upgrade at its preflight (`websocket_not_supported`), before OpenShell contacts the server.

**Monitors.** The middleware counts the bytes each sandbox sends in allowed requests (request line, visible headers and body) over a sliding hour and reports a monitor as a finding and a `monitor` record, at most once an hour per sandbox. Response bytes are not counted. Monitors never change a decision.

Records are appended to `events.jsonl` in the state directory, one JSON object per line. They keep the request's method, scheme, host, port and path, and only the size of its query string, which can hold data and credential placeholders. The event store with its hash chain will replace this file.

## Policy store

The orchestrator writes, and the middleware reads, a state directory:

| Path | Contents |
| --- | --- |
| `policies/<hex>.yaml` | A project policy file, named by the hex SHA-256 of its bytes; a file whose bytes do not match its name is rejected |
| `sandboxes/<sandbox id>.json` | `{"policy": "sha256:<hex>"}`: the revision an OpenShell sandbox is decided under |
| `events.jsonl` | The middleware's records |

`ValidateConfig` accepts a compiled sandbox policy only when its project policy is already in the store, so the gateway refuses a sandbox policy the middleware could not decide. Until the orchestrator manages the store, the command line does:

```bash
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
listen = "0.0.0.0:50051"
state_dir = "state"
ceiling = "pool-ceiling.yaml"
admin_hosts = ["gateway.sandbox-science.internal"]   # added to the hard floor

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

Register it with the gateway, which must be able to reach it, as must every sandbox supervisor; the gateway checks it at startup, so start the middleware first. The registration's `max_payload_bytes` must not exceed the middleware's own:

```toml
[[openshell.supervisor.middleware]]
name = "sandbox-science-policy"
grpc_endpoint = "https://host.openshell.internal:50051"
tls_ca_cert_path = "/path/to/middleware-ca.pem"
max_payload_bytes = 4194304
timeout = "500ms"
```

The compiler attaches it to every host in each research sandbox's policy (`examples/openshell/research-sandbox.yaml`).

OpenShell's client pings the service every 10 seconds, even between calls; the server allows this, since gRPC's defaults would answer with `too_many_pings` and the reconnects would fail requests closed.

## To verify on babbage

The facts above come from OpenShell v0.1.3's source. To confirm against a running gateway:

- traffic admitted by the host-less endpoint reaches the middleware with the requested name;
- WebSocket upgrades are denied (both the upgrade request and the preflight);
- bodies over `max_payload_bytes` are blocked (`middleware_failed`), so larger uploads fail;
- the latency of the extra call, and requests failing closed while the middleware is down;
- real git clients through the whole path, including gzip-compressed fetch bodies.
