# MCP connectors and authentication

Confirmed: D64, and D66 and D67 with the design this page proposes. The MCP proxy is built ([as built](#the-mcp-proxy-as-built)); the connector auth service is not. The user's requirements:

- crypto-research-mcp is the phase 1 connector; others are added later.
- MCP services authenticate mainly with OAuth.
- `Authorization` and custom-header authentication should also be supported.
- A secrets manager should keep raw credentials away from agents.

Sources: the [MCP authorization specification, 2025-11-25](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-11-25/basic/authorization.mdx) and the OpenShell v0.1.3 [providers](https://github.com/NVIDIA/OpenShell/blob/v0.1.3/docs/how-it-works/providers/overview.mdx) and [profiles](https://github.com/NVIDIA/OpenShell/blob/v0.1.3/docs/how-it-works/providers/profiles.mdx) documentation. The MCP proxy is tested against a fake server, and it and its compiled policy were run under OpenShell on babbage (labs 2, 13, 15 and 17). The connector auth service, the OAuth flow and the checks for the spike below are not built or tested.

## Goals

1. **No agent-reachable process holds a raw MCP credential.** That covers the harness, delegated agents and research workers.
2. **Workers never connect to MCP services directly** (user requirement). All MCP traffic, from the orchestrator and from workers, goes through one MCP proxy, which applies each project's policy.
3. **One registry for all servers.** Each server is a `mcp.servers` entry in the project policy plus an OpenShell provider. There are no server-specific code paths, so adding a server later is configuration.
4. **The same tool-level policy applies to every caller,** with an optional narrower toolset for worker code than for agents.

## Components

| Component | Zone | Role |
| --- | --- | --- |
| Connector auth service | Z1 (trusted) | Runs the interactive OAuth flow with the user, registers clients, stores tokens in OpenShell providers, and performs refreshes when the gateway cannot (below). Python, MIT, built on the MCP Python SDK's OAuth client support where it fits |
| **MCP proxy** | Runs **inside its own OpenShell sandbox** | The only MCP endpoint that the orchestrator and workers can reach. It authenticates each caller, applies the project's tool policy, and forwards to remote MCP servers through the OpenShell proxy, which injects credentials and enforces `protocol: mcp` rules again on the way out. It holds only placeholders |
| OpenShell gateway and providers | Z1 | Credential storage (encrypted database, Kubernetes Secrets or OpenBao), placeholder resolution, gateway-managed refresh where possible |
| Harness MCP client and worker MCP clients | Z2 / Z3 | Talk only to the MCP proxy; their configuration contains no secrets |

## MCP proxy

**Reachability.** Worker sandbox policy allows the MCP proxy endpoint and no MCP server hosts. Worker sandboxes carry no MCP providers. A worker that tries to reach an MCP server directly is denied by the hard floor, because MCP server hosts are listed as proxy-only.

**Caller identity and attribution.** A credential inside a sandbox identifies the *sandbox*, not the process that sent the request. OpenShell placeholders are endpoint-scoped, not process- or binary-scoped. Processes in one container usually run as the same user, so a process can read another process's environment. A per-session proxy credential placed in a shared project container could therefore be reused by any other workbook's process in that container. Attribution therefore comes in levels, and only the trusted levels may drive permission decisions:

| Level | When it applies | Trust |
| --- | --- | --- |
| `orchestrator` | Agent tool calls made by the harness. The orchestrator knows which session's agent made the call | Trusted to the degree zone Z2 is; usable for session-scoped decisions |
| `sandbox` | Worker code in a **dedicated** session container | Verified by the sandbox identity the OpenShell supervisor reports; usable for session-scoped decisions |
| `container` | Worker code in a **shared** project container | Project and container are verified. The session label is a *claim* and is recorded as such |
| `process` (to evaluate) | Shared container, if the trusted worker service can map the supervisor's reported originating process to an execution it launched (for example, one cgroup per execution that sandboxed code cannot leave) | Usable for session-scoped decisions only once the spike shows the mapping is trustworthy |

**Consequences:**

- Grants and escalation decisions for worker-originated MCP calls from a shared container apply to the **whole container**, meaning every workbook in it, unless `process`-level attribution is established.
- A workbook that needs session-scoped MCP permissions should use a dedicated container.
- The monitor shows the attribution level with every event and never treats an unverified session label as evidence.

The per-caller proxy credential still matters: it stops other sandboxes and projects from using the proxy. It is not, by itself, a per-session identity.

**Policy.** The proxy evaluates the project's `mcp.servers` tool rules (`allow`/`escalate`/`deny`, most restrictive wins) before forwarding. `escalate` creates a durable permission request through the shared workflow, with the policy's timeout and deny fallback. The outbound OpenShell `protocol: mcp` rules repeat the allow/deny lists, as defense in depth.

**Other duties:**

- Rate and spend limits per project and server (for credit-consuming tools such as crawlers).
- Response size limits. Large or binary results (PDFs, images) become artifact references in project storage instead of base64 in model context, as the external skill pack requires.
- Uniform handling of `403 insufficient_scope` (escalation, not automatic re-consent).
- One audit stream of requests and responses for the contextual monitor.

**Implementation.** A thin proxy in Python, since the policy and attribution semantics are project-specific: `packages/mcp-proxy`, with its own small JSON-RPC layer on aiohttp rather than the MCP Python SDK (D75; [as built](#the-mcp-proxy-as-built)). Existing permissively licensed proxies remain references: sparfenyuk/mcp-proxy (MIT), IBM mcp-context-forge (Apache-2.0) and docker/mcp-gateway (MIT). None was reviewed in depth.

**Placement.** One proxy per deployment, policy-scoped per project, is enough for phase 1 on babbage. On the cluster it runs as a separate sandboxed deployment near the workers.

The alternative of running the orchestrator itself as an OpenShell sandbox (D47) remains worth evaluating. The proxy is needed either way, so that workers stay off MCP services.

## The MCP proxy as built

The proxy is in `packages/mcp-proxy` (`sandbox_science.mcp_proxy`, command `sandbox-science-mcp-proxy`; D75). Its tests run it against a fake server. In its own OpenShell VM sandbox, OpenShell substitutes the provider's token in its headers, its version header and its replies to server requests pass the fence, and callers reach it from the host and from an orchestrator sandbox ([lab 13](../.agents/docs/agent-reports/babbage-openshell-lab-13-2026-10-11.md), against DeepWiki's public server and a fake one).

With the MCP Python SDK 2.2.0 as the caller and as the server behind the proxy, the client's `server/discover` probe (MCP 2026-07-28) is refused, the client falls back to `initialize`, and listing tools, an allowed call and an escalated one behave as below, over the server's event streams.

**Endpoints.** Each declared server has a Streamable HTTP endpoint, `POST /mcp/<server>`, where `<server>` is its name under `mcp.servers`. The proxy speaks MCP 2025-03-26, 2025-06-18 and 2025-11-25, to callers and to servers, and answers every request with `application/json`. `GET` answers 405, since the proxy passes no server-initiated messages on, and `DELETE` ends the caller's session.

**Sessions.** A caller's `initialize` opens a session at the proxy and a separate upstream session, which the proxy opens itself:
- It offers the revision the server's entry pins (`revision`, which OpenShell's fence enforces). Without a pin it offers the caller's revision if it speaks it, otherwise 2025-11-25. It refuses an answer with any other revision.
- It declares no client capabilities and sends its own `clientInfo`, so a server has no grounds for sampling, elicitation or roots requests.
- It completes the handshake with `notifications/initialized`. Every later request carries `MCP-Protocol-Version`, and the server's `Mcp-Session-Id` if it issued one.
- The caller gets the server's result without the `tasks` capability, since the policy denies the task methods. Its session id is the proxy's own, valid only with the same token and server.

A caller holds at most `max_sessions_per_caller` sessions; its next one ends its least recently used, so that no caller can take every place. When the proxy holds `max_sessions` sessions and the caller holds none of them, its `initialize` is refused with 503 `proxy_busy`, and it should retry later. A session ends after `session_idle` without use, on `DELETE`, when the server ends it (404), or when a new policy revision changes the server's URL or pinned revision. The caller then gets 404 and initializes again. The proxy never sends `DELETE` upstream, because OpenShell's MCP inspection refuses it.

**How a call is decided.** In this order, for every message:

| Check | Refusal |
| --- | --- |
| No `Origin` header (callers are services, not web pages), and a bearer token from the settings | 403 `origin_forbidden`, 401 `unauthenticated` |
| A JSON body within `max_body_bytes` | 413 `request_too_large` (406 or 415 for other media types) |
| A policy revision bound to the caller's project | 503 `project_unbound`, `policy_unavailable` |
| One JSON-RPC request or notification, parsed strictly: no batches, duplicate keys, unknown members or responses | 400 `request_unclassifiable` |
| The caller's own session; a revision the caller names must be the session's | 400 `session_required`, 404 `session_unknown`, 400 `protocol_unsupported` |
| The policy library, with the caller's kind as origin: protocol methods, tool rules, `worker_access`, `worker_tools`, the hard floor and the pool ceiling | the library's code, as for the middleware (`policy_denied`, `pool_ceiling_denied`, `destination_forbidden`, `request_unclassifiable`) |
| Only the parameters MCP defines for the method, besides `_meta`, which is dropped; no task-augmented requests; the parameters, as forwarded, within `mcp.proxy.max_argument_bytes` | `request_unclassifiable`, `arguments_too_large` |
| Nothing in the parameters that reads as an OpenShell placeholder | `credential_in_body` |
| A placeholder for the server's credentials in the proxy's environment | `credential_unavailable` |
| For `tools/call`, a name in the server's latest complete `tools/list`, compared exactly. The proxy lists the tools itself when it has no complete listing, and again after the server reports a change; the caller's own listings count once their last page arrives | `tool_not_advertised` |
| An escalation is a permission request (D76), as in the middleware. Its first occurrence is recorded with its key: project, caller, policy revision, server, method, tool and a digest of the parameters, never the parameters. The refusal's `data.request` names the request; retries wait on it without new records, and identical calls handled at the same time within one proxy process record one request, or get the answer it received meanwhile (two proxy processes on one store can each record it). The proxy reads the stored requests and answers on a thread, all of them before it serves, then only those added since the newest record it has read. An approval covers the call or, with `--scope tool`, the tool with any arguments; a denial refuses; unanswered past the timeout, the call is refused as timed out. If the record cannot be stored, there is no request to approve | `approval_required`, `approval_denied`, `escalation_timed_out`, `audit_unavailable` |
| An alert proceeds only once its record is stored | `audit_unavailable` |
| The server's resolved addresses pass the hard floor and the pool ceiling, checked before each new connection. Inside an OpenShell sandbox every name resolves to a synthetic address (`198.18.0.0/15`), so there the fence checks the real ones: the compiled endpoint for a server named by host name carries `allowed_ips`, the public ranges less the floor's and the ceiling's addresses (lab 13) | `destination_forbidden`, `pool_ceiling_denied` |

A refusal is a JSON-RPC error with code -31001, meaning nothing was sent, and the reason code in `data.reason`. Its message is fixed text for the code; the rule that matched stays in the proxy's log and the event store, as for the middleware. A refused notification gets an HTTP error status instead. A call that was sent but brought no usable answer gets code -31002 and `upstream_error` (a malformed answer, an error status, a timeout, a redirect), `upstream_unauthorized` (401), `upstream_unavailable`, `result_too_large` or `fence_denied`. The last is OpenShell's refusal at the proxy sandbox's fence (403 with a body naming the policy and the layer), which means the compiled policy is narrower than the project's; a server could send the same body, so the call still counts as sent. The server's own JSON-RPC errors are passed on, except ones that use the proxy's codes, which become `upstream_error`: a server may not speak with the proxy's voice.

**What goes upstream.** Only what the proxy builds:
- Its own headers: `Content-Type`, `Accept`, `Accept-Encoding: identity`, `User-Agent: sandbox-science-mcp-proxy/0.1`, `MCP-Protocol-Version`, `Mcp-Session-Id` and the credential headers. Nothing a caller sends in headers reaches a server.
- The parameters MCP defines for the method, serialized again by the proxy, and nothing else. A server whose JSON decoder ignores the case of keys, as Go's does, would otherwise read a `NAME` member as the tool name the policy never saw.
- Credentials only as headers that hold OpenShell placeholders, read from the proxy's environment: `Authorization: Bearer <placeholder>` for `bearer` and `oauth` servers, and the entry's headers for `headers` servers. A variable that holds anything but a placeholder is refused, so the proxy never sends a credential it holds itself.
- No cookies and no redirects.

Of a caller's notifications, only `notifications/cancelled` for a call in flight goes upstream, under the proxy's id for that call and without the caller's reason text. The others have nothing upstream to refer to.

**What comes back.** Answers are read within `mcp.proxy.max_response_bytes`, and never more than 32 MiB, as they arrive, as JSON or as an event stream that ends with the response (2025-03-26 servers may batch messages in one event). Compressed answers are refused. The session id a server issues must be visible ASCII with nothing that reads as a placeholder, since the proxy sends it back in a header. In a stream, the proxy answers `ping` and refuses other server requests with error -32601, at most 16 per answer. It notes `notifications/tools/list_changed` and drops the other notifications. Image and audio blocks and blob resources are replaced by a text note naming their type and size, until binary results become artifact references.

**Callers and attribution.** Each caller in the settings has a token (the settings hold its SHA-256), a kind and a project. A worker token is issued to one sandbox (`container`). A token for a sandbox dedicated to one session also names that `session`. Records carry the attribution level:
- `orchestrator`: the harness, which names the session in a `Sandbox-Science-Session` header;
- `sandbox`: worker code in a dedicated sandbox, attributed to its session. A different label in the header is recorded as a claim;
- `container`: worker code in a shared project container. The header's label is recorded only as `session_claim`.

**Records.** In the [event store](EVENT-STORE.md), with the source `mcp-proxy`:
- `escalation`: a pending permission request, with the attribution, server, MCP session, method, tool, policy revision, rule, reason, escalation terms and its `target` key;
- `approved`: a call that went through on an approval, naming the approval and the request;
- `alert`: the same fields, stored before the call proceeds;
- `mcp-call`: one record for every message an authenticated caller sends, with the same subject, the decision (action, layer, rule), the reason code, the outcome (`forwarded`, `server_error`, `refused`, `failed` or `local`), the sizes of the request, the parameters and the answer, what a stream carried, and the duration. It is written on a best-effort basis, like the middleware's monitor findings.

Argument values and results are never recorded.

**Policy store.** It uses the middleware's layout (`policies/<hex>.yaml`), and `projects/<project>.json` (`{"policy": "sha256:<hex>"}`) binds a project's MCP calls to a revision. The binding is read for every call, so a new revision applies from the next one.

### Running it

Settings are a TOML file (relative paths resolve against it):

```toml
listen = "127.0.0.1:8080"
state_dir = "state"                # the policy store, and events.db unless event_store is set
ceiling = "pool-ceiling.yaml"
admin_hosts = ["gateway.sandbox-science.internal"]   # added to the hard floor
max_body_bytes = "1Mi"             # the largest request a caller may send
max_sessions = 256
max_sessions_per_caller = 16       # a caller's next session ends its least recently used one
session_idle = "1h"
upstream_timeout = "5m"            # one call to a server, from start to end

[credentials]   # environment variables that hold OpenShell placeholders
crypto-research-mcp = "CRYPTO_RESEARCH_MCP_TOKEN"     # bearer and oauth: the provider's token
"example-header-server.API_KEY" = "EXAMPLE_API_KEY"   # headers: a ${provider:...} reference

[[callers]]
name = "orchestrator"
token_sha256 = "<64 hex digits>"
kind = "orchestrator"
project = "crypto"

[[callers]]
name = "crypto-default"
token_sha256 = "<64 hex digits>"
kind = "worker"
project = "crypto"
container = "ss-crypto-default-1a2b3c"   # the sandbox the token was issued to
# session = "s-42"                        # only for a sandbox dedicated to one session
```

```bash
uv run sandbox-science-mcp-proxy new-token   # prints a token, for the caller, and its token_sha256
uv run sandbox-science-mcp-proxy bind --state-dir state --project crypto --policy policy.yaml
uv run sandbox-science-mcp-proxy serve --settings proxy.toml
```

Callers send `Authorization: Bearer <token>` to `http://<listen>/mcp/<server>`; the MCP Python SDK's Streamable HTTP client takes it in `headers`.

**In its own sandbox.** In production the proxy runs in its own OpenShell sandbox, from the `mcp-proxy` image ([images](IMAGES.md#the-mcp-proxy-image)): OpenShell sets the credential variables to placeholders, and the compiled policy (`compile_mcp_proxy`) re-enforces the declared servers, methods and tools. The image's command, `/opt/sandbox-science/bin/mcp-proxy-run`, waits until the settings exist at `/sandbox/mcp-proxy/proxy.toml`, runs the proxy, and starts it again whenever it exits. The operator:

1. compiles the sandbox's policy with `compile_mcp_proxy()`. The targets file's `mcp_proxy` names the interpreter as the binary (`/opt/sandbox-science/bin/python*`), since OpenShell sees the interpreter that runs a Python entry point (lab 13), and in `filesystem` the image's prefix, read-only;
2. creates the sandbox from the image, with the providers the compiled policy binds and the launcher as its command;
3. uploads the pool ceiling and the project policies into `/sandbox/mcp-proxy/`, then the settings (`listen = "127.0.0.1:8080"`, `state_dir = "state"`), since the launcher starts the proxy once they are there;
4. binds each project to its policy with `bind`, through `openshell sandbox exec`;
5. exposes the proxy, which listens on the sandbox's loopback, on the pool host with `openshell forward service`. Other sandboxes then reach it as `host.openshell.internal`; the orchestrator's compiled policy admits it as `orchestrator.mcp_proxy` in the targets file.

```bash
openshell sandbox create --name mcp-proxy --from sandbox-science/mcp-proxy:<tag> \
  --policy mcp-proxy-policy.yaml --provider crypto-research-mcp --no-auto-providers --detach \
  -- /opt/sandbox-science/bin/mcp-proxy-run
openshell sandbox exec -n mcp-proxy --no-tty --no-login-shell -- mkdir -p /sandbox/mcp-proxy
for file in pool-ceiling.yaml policy.yaml proxy.toml; do   # the settings last
  openshell sandbox upload --no-git-ignore mcp-proxy "$file" /sandbox/mcp-proxy/
done
openshell sandbox exec -n mcp-proxy --no-tty --no-login-shell -- \
  /opt/sandbox-science/bin/sandbox-science-mcp-proxy bind \
  --state-dir /sandbox/mcp-proxy/state --project crypto --policy /sandbox/mcp-proxy/policy.yaml
openshell forward service --target-port 8080 --local 127.0.0.1:8080 mcp-proxy
```

- Commands in the sandbox take full paths, since OpenShell's VM driver does not apply the image's `PATH`. `pending` (`--request N` shows one request in full), `approve` and `deny` run the same way, with `--settings /sandbox/mcp-proxy/proxy.toml`, and `/opt/sandbox-science/bin/sandbox-science-events export /sandbox/mcp-proxy/state/events.db` copies the records out.
- Until a project is bound, its calls get 503 `project_unbound`. A binding, or a new revision bound later, applies from the next call, without a restart.
- The state directory, with the policy store and the event store, is on the sandbox's disk, which survives `sandbox stop` and `start`.
- The providers' placeholders reach the sandbox's command. The launcher restarts a proxy that exits, and the launcher and the proxy come back a few seconds after the sandbox is `Ready` again ([lab 15](../.agents/docs/agent-reports/babbage-openshell-lab-15-2026-10-11.md)).
- The forward ends when the sandbox stops, so whatever runs `openshell forward service` must start it again afterwards.

To keep the forward up, run it from the systemd user unit in [`examples/systemd`](../examples/systemd), whose instance is the proxy's sandbox. It starts the forward again every two seconds until the sandbox is back, without reaching systemd's start limit. The proxy itself comes back a few seconds after the sandbox is `Ready`, so a caller should retry a refused connection briefly: on babbage a call succeeded about 9 s after `sandbox start`, on its second try ([lab 17](../.agents/docs/agent-reports/babbage-openshell-lab-17-2026-10-11.md)).

```bash
cp examples/systemd/sandbox-science-mcp-proxy-forward@.service ~/.config/systemd/user/
# set the path to openshell in it, then:
systemctl --user daemon-reload
systemctl --user enable --now sandbox-science-mcp-proxy-forward@mcp-proxy.service
```

### Not built yet

- A harness MCP client. The orchestrator does not call the proxy, so agents cannot use connectors; only code in research sandboxes can, where the policy grants `worker_access`. Nothing issues proxy tokens to callers either: an operator writes them into the settings.
- A product command for the proxy's sandbox. `compile_mcp_proxy()` is a library function that only the example renderer and the lab scripts call, and the operator steps above are manual.
- Exporting the event store's records from the proxy's VM to the monitor, and a way for approvals to reach the proxy other than `openshell sandbox exec`.
- The connector auth service: OAuth with DCR, refresh, the upstream-worker swap for external refresh, and `403 insufficient_scope` as an escalation. Until then an `oauth` server works only with a token put into its provider by hand.
- Binary results as artifact references; they are withheld for now.
- Rate and spend limits per project and server.
- MCP 2026-07-28, which the policy library knows: it has no sessions, carries metadata in every request, mirrors names in `Mcp-Method` and `Mcp-Name` headers and embeds server requests in results. The proxy refuses servers pinned to it, and the policy's list of protocol methods needs it too.
- Passing server notifications (progress, logging, list changes) to callers, and resuming interrupted event streams.
- Caller identity from the platform rather than per-sandbox tokens, since the middleware knows which sandbox sent a request, and `process`-level attribution.
- Approved tools are not compiled into the proxy sandbox's fence, so OpenShell still refuses an escalated tool there; until they are, the fence has to allow escalated tools for approvals to take effect (backlog).
- `Basic` credentials in `headers` servers: how OpenShell expects a placeholder inside one is unchecked.
- A test against crypto-research-mcp, which needs OAuth.

## Authentication modes

Each server entry declares one mode:

| Mode | Use | How credentials reach the request |
| --- | --- | --- |
| `oauth` (primary) | Servers that follow MCP authorization | The access token is stored as an OpenShell provider credential with `auth_style: bearer`. The MCP proxy sends `Authorization: Bearer <placeholder>`, and the OpenShell proxy substitutes the real token on every request (the spec requires the header on every request and forbids tokens in query strings) |
| `bearer` | Static API tokens | Provider credential; `Authorization: Bearer <placeholder>` |
| `headers` | Custom header schemes (`X-Api-Key`, `Authorization: Basic …`, several headers) | One provider credential per secret value. Header values in the server entry reference them, and the MCP proxy places the placeholders. OpenShell resolves placeholders in any header value, including Basic auth. The roadmap's "profile-driven placement" is not needed, because the MCP proxy places the placeholder |
| `none` | Public servers | No credential |

Checked on babbage ([lab 2](../.agents/docs/agent-reports/babbage-openshell-lab-2-2026-10-10.md)):
- A provider named by `credential_binding` must be attached to the MCP proxy's sandbox, and its profile must declare no endpoints.
- OpenShell substitutes placeholders in headers, never in a JSON-RPC body, which it forwards unchanged.
- After `initialize`, the proxy must send `MCP-Protocol-Version` on every request. Without it, OpenShell assumes 2025-03-26 and refuses requests that a policy limits to later revisions.

## OAuth flow (MCP 2025-11-25)

The connector auth service performs the client side of MCP authorization when the user clicks **Connect** in the GUI:

1. **Discovery.** Call the server unauthenticated, read the `WWW-Authenticate` resource-metadata URL (or fall back to the well-known paths), fetch the protected-resource metadata (RFC 9728), then the authorization-server metadata (RFC 8414 / OpenID discovery).
2. **Client registration**, in the order the spec prefers:
   - *Client ID Metadata Document*: needs an HTTPS-hosted metadata document. Practical once there is a hosted endpoint.
   - *Pre-registration*: a configured `client_id` and secret.
   - *Dynamic Client Registration* (RFC 7591): the spec's fallback. **crypto-research-mcp supports it (confirmed), so phase 1 uses DCR.** The connector auth service registers once per deployment and redirect URI: a `localhost` callback on babbage, a separate HTTPS registration when hosted. It stores the registration response (`client_id`, any `client_secret` or registration access token) as secret refresh material, and re-registers if the authorization server later rejects the client.
3. **Authorization code with PKCE.** Include the `resource` parameter set to the server's canonical URI (RFC 8707). Use the policy's scopes; if the policy lists none, use the spec's scope selection (the `scope` in `WWW-Authenticate`, else `scopes_supported` from the protected-resource metadata), and record the granted scopes. Use a `localhost` redirect on babbage. Hosted deployments use an HTTPS callback, which the spec requires for non-localhost redirects.
4. **Token exchange**, again with `resource`.
5. **Storage.** The access token becomes the provider's injectable credential, with its expiry. The refresh token, `client_id` and any client secret become secret refresh material. The token endpoint goes into a per-server provider profile; OpenShell keeps token URLs profile-owned.

**Refresh.** There are two options, and the spike decides between them per server:

- *Gateway-managed* (`oauth2_refresh_token`). OpenShell refreshes the token and stores rotated refresh tokens automatically. Its documented refresh configuration has a token URL and scopes, but **no `resource` parameter**, which the MCP spec requires on every token request. This works only if the authorization server accepts refreshes without `resource`.
- *External* (`external` strategy). The connector auth service refreshes with `resource` and pushes the new access token to the provider through the OpenShell API. Refresh material then lives in the connector auth service's secret storage (Kubernetes Secrets/OpenBao, or the gateway's store if the API allows).

  **Reloading the running proxy.** OpenShell documents that after a static credential update, a process that is already running keeps its *revision-scoped* placeholder, and that placeholder does not resolve to the new value; a new process is needed. Gateway-managed refresh uses stable refresh handles, so later rotations need no restart. With external refresh, the MCP proxy must therefore replace its upstream client for that server on every token update. Design:
  - The MCP proxy runs one **upstream worker process per MCP server**. The front-end process that accepts callers holds no credential placeholders.
  - The connector auth service updates the provider with `--wait`, which reports per-sandbox application of the change, and then notifies the MCP proxy with the change ID.
  - The proxy starts a new upstream worker, which receives the new revision's placeholder, routes new calls to it, lets in-flight calls on the old worker finish within a grace period, then stops the old worker.
  - Refresh runs ahead of expiry (`refresh_before`) so the switch happens while the old token is still valid. A call that still fails with 401 on an old worker is retried once on the new worker, only for idempotent reads.
  - The spike checks whether the `external` strategy also gets stable handles. If it does, the worker swap becomes unnecessary.

**Scope changes.** If a server answers `403 insufficient_scope`, the proxy does not re-authorize on its own. It raises a permission escalation naming the scopes requested. The user re-consents through the connector auth service. This keeps step-up authorization a human decision.

**Token audience.** Because tokens are audience-bound to one MCP server (RFC 8707) and the OpenShell provider is endpoint-bound to the same host, a token cannot be replayed at another service even inside the proxy.

## Server entry (policy file)

```yaml
mcp:
  servers:
    crypto-research-mcp:
      url: https://…/mcp                 # canonical URI, used as the OAuth resource
      revision: "2025-11-25"             # MCP protocol revisions allowed
      auth:
        mode: oauth
        registration: dcr                # cimd | preregistered | dcr
        scopes: [read]                   # least privilege; server-specific names
        refresh: gateway                 # gateway | external
      tools: { allow: [...], escalate: [...], deny: [...] }

    example-header-server:               # later phase
      url: https://…/mcp
      auth:
        mode: headers
        headers:
          X-Api-Key: "${provider:example-header-server.API_KEY}"
          X-Account: "research"           # non-secret literal
```

`${provider:NAME.KEY}` references resolve to OpenShell placeholders, never to secret values. A literal secret in a header value is rejected by the policy compiler.

## Later phases

- **Local stdio MCP servers** run inside a worker sandbox, with their own providers and egress policy. They are never spawned beside the orchestrator.
- **More remote servers** need only a policy entry and a **Connect** click (OAuth), or a provider created from a token (bearer/headers).
- **Per-project token separation.** The same service connected for two projects can use two providers, so revoking one project's access does not affect the other.

## Checks for the spike

| Check | Required evidence |
| --- | --- |
| OAuth connect to crypto-research-mcp | Discovery, registration, PKCE and `resource` parameter recorded; tokens stored only in the provider/credential driver |
| Placeholder injection | The proxy and sandbox environment hold placeholders only; requests reach the server with a valid bearer token |
| Refresh | Gateway-managed refresh succeeds across several expiries without restarting the proxy, or a server rejection without `resource` is observed and external refresh succeeds. With external refresh, calls continue across token expiry through the upstream-worker swap with no failed calls beyond the documented read retry, and an old worker's placeholder is confirmed stale after the update. Rotated refresh tokens persist |
| Custom headers | A fixture server requiring `X-Api-Key` and one requiring Basic auth both accept placeholder-injected values; the unbound host gets `credential_endpoint_mismatch` |
| Tool policy | Allowed, escalated and denied tools behave as configured for calls from the orchestrator and from a worker, both through the proxy |
| No direct worker access | A worker request to the MCP server host is denied; the worker environment contains no MCP placeholders |
| Attribution | Two sessions in one shared container call the same tool: events show `container`-level attribution, with the session as a claim, and a grant made for one applies to both. The same calls from a dedicated container show `sandbox`-level attribution. Evaluate `process`-level mapping (supervisor originating-process info plus per-execution cgroups) and try to defeat it from a second process |
| Binary results | A PDF resource returned through the proxy becomes an artifact reference, not inline base64 |
| Step-up | A `403 insufficient_scope` becomes a permission request, not an automatic re-authorization |
| Revocation | Disconnecting a server removes the provider; later calls fail closed |
