# MCP connectors and authentication

Confirmed: D64, and D66 and D67 with the design this page proposes. The MCP proxy is built ([as built](#the-mcp-proxy-as-built)); the connector auth service is not, and M2 does not need it. The user's requirements:

- crypto-research-mcp is the phase 1 connector; others are added later.
- MCP services authenticate mainly with OAuth.
- `Authorization` and custom-header authentication should also be supported.
- A secrets manager should keep raw credentials away from agents.

**crypto-research-mcp uses a Cloudflare Access service token, not OAuth** (agreed by the owner, 2026-10-14). It is a `headers` server: its two header values reference two credentials of one OpenShell provider, which the owner creates. No secret passes through an agent, the harness or this repository ([below](#crypto-research-mcp-a-cloudflare-access-service-token)). The server is at `https://research-mcp-api.npages.org/mcp`, and the service token alone is enough: no OAuth bearer is needed behind Access ([checked on 2026-10-14](#what-the-server-offers)). The connector auth service and the OAuth flow stay the design for later OAuth servers, off M2's path.

Sources: the [MCP authorization specification, 2025-11-25](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-11-25/basic/authorization.mdx) and the OpenShell v0.1.3 [providers](https://github.com/NVIDIA/OpenShell/blob/v0.1.3/docs/how-it-works/providers/overview.mdx) and [profiles](https://github.com/NVIDIA/OpenShell/blob/v0.1.3/docs/how-it-works/providers/profiles.mdx) documentation. The MCP proxy is tested against a fake server, and it and its compiled policy were run under OpenShell on babbage (labs 2, 13, 15 and 17). The connector auth service, the OAuth flow and the checks for the spike below are not built or tested. A direct `initialize` and tool listing with the service token worked on 2026-10-14. Through the proxy, in its sandbox with the provider attached, a run listed crypto-research-mcp's tools and called its e-print tools on 2026-10-15 ([lab 39](../.agents/docs/agent-reports/babbage-openshell-lab-39-2026-10-15.md)).

## Goals

1. **No agent-reachable process holds a raw MCP credential.** That covers the harness, delegated agents and research workers.
2. **Workers never connect to MCP services directly** (user requirement). All MCP traffic, from the orchestrator and from workers, goes through one MCP proxy, which applies each project's policy.
3. **One registry for all servers.** Each server is a `mcp.servers` entry in the project policy plus an OpenShell provider. There are no server-specific code paths, so adding a server later is configuration.
4. **The same tool-level policy applies to every caller,** with an optional narrower toolset for worker code than for agents.

## Components

| Component | Zone | Role |
| --- | --- | --- |
| Connector auth service (later; not for crypto-research-mcp) | Z1 (trusted) | Runs the interactive OAuth flow with the user, registers clients, stores tokens in OpenShell providers, and performs refreshes when the gateway cannot (below). Python, MIT, built on the MCP Python SDK's OAuth client support where it fits. Needed only for OAuth servers; crypto-research-mcp uses a service token the owner registers |
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

- Rate limits per project and server are not built. Cost control belongs to the MCP services, not to this project: a service limits and bills its own use, and a tool is escalated for its effects, never for its cost (owner, 2026-10-14). Project-side cost controls are a low-priority backlog item.
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

**What a listing offers.** A caller's `tools/list` page leaves out the tools the project's policy denies, each judged as a call by its name alone, so an agent is not offered a tool it can never call (lab 39, B5); escalated tools stay, since they ask the operator. The call's record counts them (`denied_tools`). The proxy's own record of the server's tools keeps them all, and a call to a left-out tool is refused as before.

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
# headers: one per ${provider:NAME.KEY} reference; the variable is the credential's key
"crypto-research-mcp.CF_ACCESS_CLIENT_ID" = "CF_ACCESS_CLIENT_ID"
"crypto-research-mcp.CF_ACCESS_CLIENT_SECRET" = "CF_ACCESS_CLIENT_SECRET"
# bearer and oauth servers: the provider's token
# example-oauth-server = "EXAMPLE_OAUTH_SERVER_TOKEN"

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

**In its own sandbox.** `sandbox-science up` does the steps below when its config has `[mcp_proxy]` ([quickstart](QUICKSTART.md#the-mcp-proxy)). In production the proxy runs in its own OpenShell sandbox, from the `mcp-proxy` image ([images](IMAGES.md#the-mcp-proxy-image)): OpenShell sets the credential variables to placeholders, and the compiled policy (`compile_mcp_proxy`) re-enforces the declared servers, methods and tools. The image's command, `/opt/sandbox-science/bin/mcp-proxy-run`, waits until the settings exist at `/sandbox/mcp-proxy/proxy.toml`, runs the proxy, and starts it again whenever it exits. The operator:

1. compiles the sandbox's policy with `sandbox-science-mcp-proxy compile-policy` (below), from the project policy, the pool ceiling and the targets file. The targets file's `mcp_proxy` names the interpreter as the binary (`/opt/sandbox-science/bin/python*`), since OpenShell sees the interpreter that runs a Python entry point (lab 13), and in `filesystem` the image's prefix, read-only. Without options, the fence denies escalated tools, and OpenShell refuses one even after the operator approves it. `--admit-escalated`, which is off by default, admits at the fence only the tools the policy escalates by name (not those escalated by `defaults.action`), and the proxy still holds each of their calls for approval. An escalated pattern needs OpenShell deny entries beside it, and OpenShell's deny entries win over an exact allow: when admitting the patterns would deny a tool the policy allows by exact name, as `portal_*` would deny `portal_list_servers` in the example, the compiler admits only the escalated exact names and warns that the patterns' tools stay denied at the fence. The flag never denies a tool the fence admits without it;
2. creates the sandbox from the image, with the providers the compiled policy binds and the launcher as its command;
3. uploads the pool ceiling and the project policies into `/sandbox/mcp-proxy/`, then the settings (`listen = "127.0.0.1:8080"`, `state_dir = "state"`), since the launcher starts the proxy once they are there;
4. binds each project to its policy with `bind`, through `openshell sandbox exec`;
5. exposes the proxy, which listens on the sandbox's loopback, on the pool host with `openshell forward service`. Other sandboxes then reach it as `host.openshell.internal`; the orchestrator's compiled policy admits it as `orchestrator.mcp_proxy` in the targets file.

```bash
sandbox-science-mcp-proxy compile-policy --policy policy.yaml --ceiling pool-ceiling.yaml \
  --targets targets.yaml --out mcp-proxy-policy.yaml
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

### The harness's client

Agents use connectors through the harness, a caller of kind `orchestrator` (`orchestrator/connectors.py`, D-08). `run` and `serve` take the proxy's address, a file holding the harness's caller token, the project that token is for at the proxy, and the servers whose tools runs get:

```bash
sandbox-science-harness run ... --mcp-proxy http://127.0.0.1:8080 --mcp-token-file mcp-proxy.token \
  --mcp-project crypto --mcp-server crypto-research-mcp \
  --mcp-about "e-print-mcp=IACR ePrint papers: search, records, full text and PDFs" \
  --mcp-index e-print-mcp --mcp-index nist-csrc-mcp "task"
```

- **One project.** The proxy knows a token as one project's caller, and applies that project's policy and approvals to every call, so only runs of `--mcp-project` get the servers' tools. The address is `https`, or `http` on loopback or `host.openshell.internal`, the route from a sandbox to the proxy forwarded on its own host, whose traffic stays on that host; the token is one line of visible ASCII, and a token file others can read draws a warning.
- **Listing.** At a run's start, the harness lists each server's tools through the proxy, within 15 s a server, under the names the server lists. crypto-research-mcp lists 72 tools by bare names, such as `e-print-mcp_search_eprint` (lab 39, 2026-10-15). A name a run's records cannot hold (a letter or digit, then letters, digits, `_` and `-`, at most 64), one another server's tool has, or one the harness gives a tool of its own, whether the run has it or not (`python`, `restart_kernel`, `admit_hosts`, `document_reply`, `plan_read`, …), is left out and logged, and takes no place in the server's count. A server offers at most 128 tools, and a run at most 256, with at most 512 KiB of descriptions and schemas. A server that cannot be listed in time offers nothing, and the run goes on.
- **An index, and tools loaded on demand** (lab 39, B5). Every connector tool's schema at every model call cost about 17,000 tokens for crypto-research-mcp's 72 tools, and a research session could not fit its budget. So MAIN first gets an index, in the description of the harness's own `mcp_tools` tool: each server, with the operator's word on what it is for (`--mcp-about SERVER=TEXT`, at most 200 characters), and each tool's full name and the first sentence of the server's description of it, at most 60 characters, framed as the server's. Behind a portal, a tool is indexed under the server its name starts with (`e-print-mcp` of `e-print-mcp_search_eprint`, a prefix with a hyphen); otherwise under the configured server. With `--mcp-index SERVER`, repeated, the index lists those servers' tools one by one and the others by their count alone. `mcp_tools` loads tools by `names` (at most 16) or a `query`'s best matches (at most 5, scored on names, purposes and servers' words) into the run's tools for the rest of the run, and answers which it loaded, which were loaded before and which no listed tool has. A connector tool called before it is loaded is loaded then, and called. Loaded schemas count toward the run's context as any tool's do, from the next model call. The run's record keeps, under `summaries.connectors`, how many tools were listed and which were loaded, in order; each `mcp_tools` call's record says what it loaded. Measured with lab 39's tool list: 67,303 characters before; 7,257 after with every server's tools listed one by one, and 3,193 with three (`packages/orchestrator/tests/test_connectors.py`).
- **Effects.** Every connector tool has the effect class `effect`, since the policy marks none read-only, so the plan fence applies to it. `mcp_tools` is a `read`: it changes only what the run's model calls carry.
- **Untrusted text.** A tool's description loses control, invisible and private-use characters (but line breaks and tabs), is bounded to 2,000 characters and is framed as the server's. Its input schema is rebuilt from JSON Schema's structural keywords only (`type`, `properties`, `required`, `items`, `additionalProperties` as a boolean, `anyOf`/`oneOf`/`allOf`, `enum`/`const` with bounded and cleaned values, at most 1,024 characters of them a tool, past which they are dropped, and numeric and length limits); titles, descriptions, defaults, examples, patterns and references are dropped. A schema that is larger than 16 KiB, malformed, or not an object becomes an empty object schema, so a model API never refuses it.
- **Calls.** Each call names the run's session in `Sandbox-Science-Session`, which the proxy records with the call. The harness keeps one MCP session per server, started once for concurrent callers, and starts it again once if the proxy no longer knows it; callers that find it forgotten together share one new start. It sends every message with its keys sorted, so a call made again after its approval matches the request the operator approved. A call has 330 s, longer than the proxy's `upstream_timeout`.
- **Refusals.** A refusal is the tool's error, with the proxy's reason. A call held for approval comes back as `approval_required`, naming the permission request; the operator answers it with `pending` and `approve` (owner decision 1), and the agent calls again with the same arguments. An approval takes effect only for a tool the proxy sandbox's fence admits, which by default it does for no escalated tool (`--admit-escalated`, above). An error a server answered is framed as the server's. Arguments JSON cannot hold are `not_sent`, and nothing reaches the server. A call whose answer is lost or unreadable is `no_usable_answer`: the server may have done the work, and the agent is told to check before calling again.
- **Results** are the text the server returned, read whole up to 8 MiB and bounded as any tool result; the proxy withholds binary content.

Tested against the proxy and its fake server in one process (`packages/orchestrator/tests/test_connectors.py`), with synthetic tokens.

### Not built yet

- Issuing proxy tokens to callers: `sandbox-science up` makes the run service's token and gives the proxy its digest, but any other caller's token an operator writes into the proxy's settings, and the harness takes its own from a file ([the harness's client](#the-harnesss-client)).
- Exporting the event store's records from the proxy's VM to the monitor, and a way for approvals to reach the proxy other than `openshell sandbox exec`.
- The connector auth service: OAuth with DCR, refresh, the upstream-worker swap for external refresh, and `403 insufficient_scope` as an escalation. Until then an `oauth` server works only with a token put into its provider by hand. crypto-research-mcp does not need it.
- Binary results as artifact references; they are withheld for now.
- Rate limits per project and server. Cost control belongs to the MCP services; a project-side control is a low-priority backlog item.
- MCP 2026-07-28, which the policy library knows: it has no sessions, carries metadata in every request, mirrors names in `Mcp-Method` and `Mcp-Name` headers and embeds server requests in results. The proxy refuses servers pinned to it, and the policy's list of protocol methods needs it too.
- Passing server notifications (progress, logging, list changes) to callers, and resuming interrupted event streams.
- Caller identity from the platform rather than per-sandbox tokens, since the middleware knows which sandbox sent a request, and `process`-level attribution.
- Approved tools compiled into the proxy sandbox's fence one by one. Until then, `compile-policy --admit-escalated` admits at the fence the tools the policy escalates by name, but not the patterns whose deny entries would deny an exact-allowed tool, and the proxy alone holds each call for approval (backlog).
- `Basic` credentials in `headers` servers: how OpenShell expects a placeholder inside one is unchecked.

## Authentication modes

Each server entry declares one mode:

| Mode | Use | How credentials reach the request |
| --- | --- | --- |
| `oauth` (primary) | Servers that follow MCP authorization | The access token is stored as an OpenShell provider credential with `auth_style: bearer`. The MCP proxy sends `Authorization: Bearer <placeholder>`, and the OpenShell proxy substitutes the real token on every request (the spec requires the header on every request and forbids tokens in query strings) |
| `bearer` | Static API tokens | Provider credential; `Authorization: Bearer <placeholder>` |
| `headers` | Custom header schemes (`X-Api-Key`, `Authorization: Basic …`, several headers), such as crypto-research-mcp's Cloudflare Access service token | One provider credential per secret value, all in one provider (OpenShell binds one provider per endpoint). Header values in the server entry reference them, and the MCP proxy places the placeholders. OpenShell resolves placeholders in any header value, including Basic auth. The roadmap's "profile-driven placement" is not needed, because the MCP proxy places the placeholder |
| `none` | Public servers | No credential |

Checked on babbage ([lab 2](../.agents/docs/agent-reports/babbage-openshell-lab-2-2026-10-10.md)):
- A provider named by `credential_binding` must be attached to the MCP proxy's sandbox, and its profile must declare no endpoints.
- OpenShell substitutes placeholders in headers, never in a JSON-RPC body, which it forwards unchanged.
- After `initialize`, the proxy must send `MCP-Protocol-Version` on every request. Without it, OpenShell assumes 2025-03-26 and refuses requests that a policy limits to later revisions.

## crypto-research-mcp: a Cloudflare Access service token

crypto-research-mcp sits behind Cloudflare Access. A request passes Access when it carries a service token's two values in the headers `CF-Access-Client-Id` and `CF-Access-Client-Secret`, and Access then forwards it to the server. The server entry is therefore a `headers` server whose two values reference two credentials of one OpenShell provider, `crypto-research-mcp`:

```yaml
crypto-research-mcp:
  url: https://research-mcp-api.npages.org/mcp   # the Access-protected address of the server
  revision: "2025-11-25"
  auth:
    mode: headers
    headers:
      CF-Access-Client-Id: "${provider:crypto-research-mcp.CF_ACCESS_CLIENT_ID}"
      CF-Access-Client-Secret: "${provider:crypto-research-mcp.CF_ACCESS_CLIENT_SECRET}"
```

The policy compiler binds the server's endpoint to that one provider (`credential_binding`), and the MCP proxy, in its sandbox, reads the two placeholders from `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` (its settings' `[credentials]`, [above](#running-it)) and sends them as the two headers. OpenShell substitutes the values as the request leaves, only toward the bound host. The proxy holds placeholders only, and nothing an agent or a worker can reach holds the token.

**What the owner does.** The owner creates the token and the Access policy, and registers the secrets with OpenShell, on babbage. No secret passes through an agent, a chat, this repository or a command line:

1. In Cloudflare Zero Trust, create a service token for the harness (Access → Service credentials → Service Tokens), and keep its Client ID and Client Secret until step 4: Cloudflare shows the secret only once. Choose its duration, and note when it expires.
2. In the Access application that protects crypto-research-mcp, add a policy with the action **Service Auth** that includes this service token. A **Service Auth** policy admits a request with the token's headers and no user login.
3. Import the provider profile, which declares the two credentials and no endpoints (OpenShell refuses a `credential_binding` to a provider whose profile has endpoints, lab 2). Profiles are read from the gateway, so this needs the running gateway; `lint` checks the file first:

   ```bash
   openshell provider profile lint -f examples/openshell/profiles/cloudflare-access.yaml
   openshell provider profile import -f examples/openshell/profiles/cloudflare-access.yaml
   ```
4. Create the provider from environment variables, read without echo, so the values appear in no command line or shell history. `--credential KEY` without `=VALUE` takes the value from the environment variable `KEY` (OpenShell 0.1.3, `openshell provider create --help` on babbage, 2026-10-14):

   ```bash
   read -rs -p "Client ID: " CF_ACCESS_CLIENT_ID; echo; export CF_ACCESS_CLIENT_ID
   read -rs -p "Client Secret: " CF_ACCESS_CLIENT_SECRET; echo; export CF_ACCESS_CLIENT_SECRET
   openshell provider create --name crypto-research-mcp --type cloudflare-access \
     --credential CF_ACCESS_CLIENT_ID --credential CF_ACCESS_CLIENT_SECRET
   unset CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET
   ```
5. Attach the provider to the MCP proxy's sandbox (`--provider crypto-research-mcp` when it is created, [above](#running-it)), and put the URL of the server in the project policy's entry.

**From the owner's file** (approved by the owner, 2026-10-15). Instead of typing the values in at steps 3 and 4, the owner keeps them in a file in the owner's secrets directory on babbage, which no agent opens. A helper registers the provider from it:

```bash
uv run python -I -m sandbox_science.launcher.access_provider /path/to/the/owners/cloudflare.env \
  --profile examples/openshell/profiles/cloudflare-access.yaml --openshell "$HOME/.local/opt/openshell-0.1.3/bin/openshell"
```

- **The file.** The helper refuses a file that is not a regular file, that is a symbolic link, that has more than one link, that is not the user's, or that its group or others may read or write. Its directory must be the user's, and its group and others may not write it. A symbolic link is refused by its path alone: name what it links to.
  - It holds `KEY=VALUE` lines, blank lines and `#` comments.
  - The keys are `CF-Access-Client-Id` and `CF-Access-Client-Secret`, or `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET`. Case doesn't matter, nor does `-` against `_`. A `:` may come before the `=`.
  - A value may have spaces and quotes around it. Other keys are ignored.
  - A file missing either value is refused, with the missing key named.
- **What it does.** It runs `openshell provider profile lint` and `import` if the gateway has no `cloudflare-access` profile yet. Then it runs `openshell provider create`, as at step 4, or `provider update … --wait` if the provider exists.
- **What it checks first.** Whoever names the file also names the OpenShell CLI that gets the values, so the helper refuses:
  - to run with a `FAKE_*` variable set, with which the tests' stand-ins record what they are given, or an `LD_*` one, which loads code into a program;
  - an OpenShell CLI that is not an absolute path, is not a regular file the user or root owns, may be written by its group or others, or is inside a git checkout, such as this repository or one of its worktrees. `up` passes the CLI as running it would find it: a path made absolute, or a name found on `PATH`.
- **Where the values go.** Only to that one `create` or `update` command, through its environment, and from it to OpenShell's own encrypted store, the gateway's database. OpenShell 0.1.3 takes a credential from a variable or from its command line only (its `--help`), so while that command runs, the user's other processes can read the values in its `/proc/<pid>/environ`. They never appear on a command line, in a log, in the helper's own environment or in its output. Nothing that command says is shown, since it could echo them. Neither the helper nor its children write a core file, and the helper cannot be traced or read through `/proc` by the user's other processes.
- **The default on babbage, for now** (the owner, 2026-10-15). `[mcp_proxy] provider_env_file` names the file, in a config outside the repository: the tracked example has a placeholder, and a lab's config, written by the lab setup, is the only one that names the real file ([labs](../.agents/labs/README.md)). `sandbox-science up` then runs the helper itself, in a process of its own, whenever the gateway lacks the provider, as each new lab gateway does. `up --refresh-provider` registers the provider again, as after a rotation ([quickstart](QUICKSTART.md#the-mcp-proxy)). The launcher never opens the file. Steps 3 and 4 remain the alternative.

**Rotation.** A new token goes in the same way with `openshell provider update crypto-research-mcp --credential CF_ACCESS_CLIENT_ID --credential CF_ACCESS_CLIENT_SECRET --wait`, or from the owner's file with `sandbox-science up --refresh-provider`. A running process keeps the placeholder of the credential's revision it started with, so the MCP proxy's sandbox is restarted after the update ([refresh](#oauth-flow-mcp-2025-11-25), the same rule). Revoking the token in Cloudflare stops access at once; deleting the provider (`openshell provider delete crypto-research-mcp`) stops the proxy from sending it.

**Checked on babbage, 2026-10-14:** the `openshell provider create`, `update` and `profile` commands and flags above, from the installed 0.1.3's `--help`; no provider, profile or sandbox was created. The profile's fields follow OpenShell 0.1.3's profile documentation (`auth_style: header` with `header_name`); its `lint` needs the gateway. **Checked on babbage, 2026-10-15** ([lab 39](../.agents/docs/agent-reports/babbage-openshell-lab-39-2026-10-15.md)): `up` ran the helper against a new lab gateway, which linted and imported the profile and created the provider, and the proxy reached the server with it. `provider profile list -o json` answers a JSON array of profiles, each named by `id`, where the helper looks; importing a profile that exists fails with "custom provider profile '…' already exists" on stderr, which the helper takes as success.

### What the server offers

Checked on 2026-10-14 with the owner's service token, outside the proxy:

- **Access alone works.** An MCP `initialize` carrying `CF-Access-Client-Id` and `CF-Access-Client-Secret` got a normal answer (server `cloudflare-mcp-portal`, protocol 2025-11-25). Without a valid client ID the server answers 401 with an OAuth `WWW-Authenticate` challenge, so OAuth stays a possible second route, and nothing needs it.
- **What the token sees.** After the owner added a Service Auth policy for each server, the token sees 72 tools from seven servers: `attack-catalog-mcp`, `e-print-mcp`, `firecrawl-mcp`, `nist-csrc-mcp`, `paper-search-mcp`, `patent-search-mcp` and `web-fetch-mcp`; and three portal tools (`portal_list_servers`, `portal_toggle_servers`, `portal_toggle_single_server`).
- **Cost and escalation.** The services are self-hosted and effectively free, and cost control is theirs in any case. The policy escalates a tool for what it does (feedback, corpus refreshes, live browser sessions, monitors), not for what it costs. A project policy that allows all of the research servers' tools escalates none of them, and then `compile-policy --admit-escalated` is not needed for this server. The `portal_*` tools that change which servers are enabled stay denied to agents.
- **The example policy.** `examples/policy.yaml` was checked against the 78 tools listed on 2026-10-10 (a test fixture pins each tool's outcome). The listing of 2026-10-15 had 72: the seven `nist-mcp_*` tools were gone, and `firecrawl_monitor_delete` was new. A tool the policy does not name asks first, so a server revision that adds tools stays safe.

### The harness MCP client: decisions (D-08)

The proxy side and the harness's client are built ([the harness's client](#the-harnesss-client)). Decided on 2026-10-14 by the coordinator under the owner's autonomy mandate; question 6 was answered by a check:

1. **How agents see connector tools.** *Decided, as recommended:* the tools each configured server lists through the proxy, listed at a run's start and offered to MAIN under their prefixed names, each with the effect class `effect`, so the plan fence applies. *Amended on 2026-10-15 (lab 39, B5):* the proxy's listing leaves out the tools the policy denies, such as the `portal_*` tools, so they are not offered at all.
2. **The orchestrator's proxy token.** *Decided, as recommended:* the orchestrator's token goes in a file only the harness reads, like the worker token, until callers are identified by the platform (backlog).
3. **Escalated tools.** *Decided:* through the proxy's existing escalation path. An escalated call is refused with `approval_required` and recorded as a permission request; the operator answers with `sandbox-science-mcp-proxy pending` and `approve` or `deny` (owner decision 1: approvals through the CLI for M2), and the agent sees the refusal until then, as for any other escalation, and calls again. For an approval to take effect, the proxy sandbox's fence has to admit the tool: `compile-policy --admit-escalated`, opt-in and off by default, admits the tools the policy escalates by name, until approved tools are compiled into the fence one by one (above, backlog). A policy that escalates no tool of a server needs no such admission for it.
4. **Results.** *Decided, as recommended:* text results bounded as tool results are now; binary results as artifact references once that is built.
5. **A product command for the proxy's sandbox**, replacing the lab script and the manual steps above (D-08, step 3). *Decided, as recommended.*
6. **Whether crypto-research-mcp accepts Access alone.** *Answered by a check on 2026-10-14:* it does. The service token is all it needs, so the connector auth service stays off M2's path ([what the server offers](#what-the-server-offers)).

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
    example-oauth-server:                # later phase; crypto-research-mcp is a headers server (above)
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
| crypto-research-mcp through Access | With the owner's provider attached, a `tools/list` and an allowed tool call through the proxy succeed; the proxy's sandbox holds placeholders only; a call without the provider is refused by Access |
| OAuth connect (a later OAuth server) | Discovery, registration, PKCE and `resource` parameter recorded; tokens stored only in the provider/credential driver |
| Placeholder injection | The proxy and sandbox environment hold placeholders only; requests reach the server with a valid bearer token |
| Refresh | Gateway-managed refresh succeeds across several expiries without restarting the proxy, or a server rejection without `resource` is observed and external refresh succeeds. With external refresh, calls continue across token expiry through the upstream-worker swap with no failed calls beyond the documented read retry, and an old worker's placeholder is confirmed stale after the update. Rotated refresh tokens persist |
| Custom headers | A fixture server requiring `X-Api-Key` and one requiring Basic auth both accept placeholder-injected values; the unbound host gets `credential_endpoint_mismatch` |
| Tool policy | Allowed, escalated and denied tools behave as configured for calls from the orchestrator and from a worker, both through the proxy |
| No direct worker access | A worker request to the MCP server host is denied; the worker environment contains no MCP placeholders |
| Attribution | Two sessions in one shared container call the same tool: events show `container`-level attribution, with the session as a claim, and a grant made for one applies to both. The same calls from a dedicated container show `sandbox`-level attribution. Evaluate `process`-level mapping (supervisor originating-process info plus per-execution cgroups) and try to defeat it from a second process |
| Binary results | A PDF resource returned through the proxy becomes an artifact reference, not inline base64 |
| Step-up | A `403 insufficient_scope` becomes a permission request, not an automatic re-authorization |
| Revocation | Disconnecting a server removes the provider; later calls fail closed |
