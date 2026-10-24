<!-- SPDX-License-Identifier: MIT -->
# MCP through OpenShell and the middleware: the D98 prototype

Status: prototype built and tested offline (2026-10-23, backlog A2); not run against a live gateway. The decision it informs is D98's ([decisions](../DECISIONS.md)); the proxy stays as built until the owner decides.

Today every MCP call goes through the MCP proxy, a service in its own sandbox that holds the servers' credentials and decides each call ([MCP connectors](../MCP-CONNECTORS.md)). D98 would drop it: the agent's MCP client calls the server's own URL, and OpenShell's proxy, which already carries every sandbox request, checks it twice: its native `protocol: mcp` rules allow or deny each tool by name, and the policy middleware decides the rest, as it decides research sandboxes' HTTP ([policy middleware](../POLICY-MIDDLEWARE.md)). D98 names five proofs, plus response inspection; if all pass, the proxy retires; if holds do not fit, it stays, slimmed to MCP only.

## What was built

- **Compiler.** `compile_agent_mcp` (`policy/openshell.py`) gives a sandbox one inspected `protocol: mcp` endpoint per declared server, the same endpoint the proxy sandbox's fence has, from the same code, plus the policy middleware for the servers' hosts. The config names the policy revision, the project and the caller (`{"mcp": {"project": "crypto", "caller": "agent-runtime"}}`). Escalated tools are admitted by name at the fence, since the middleware holds them; denied tools are never admitted.
- **Middleware.** For a sandbox with that config (`policy_middleware/mcp.py`, `evaluator.py`), every request must be one POST of one strict JSON-RPC message to a declared endpoint, read with the code the proxy uses (`policy/mcp_wire.py`, moved from the proxy: strict JSON, the parameters MCP defines, the parameters' digest, placeholders in bodies, tool listings, event streams). The library's tool rules decide it with the orchestrator as origin, and every message gets an `mcp-call` record.
- **Harness.** `DirectClient` (`orchestrator/mcp_direct.py`) speaks Streamable HTTP to the server's URL through OpenShell's proxy, with placeholders from its environment as credential headers, and turns OpenShell's refusals into the errors the proxy's client raises, so `Connectors` is unchanged. It is not wired into the command line.
- **Responses.** With pinned tool lists (`mcp_tool_pins` in the middleware's settings), the service declares `HTTP_RESPONSE` before return and reads `tools/list` answers as they pass.

## The proofs

| Proof | Shown offline | Not shown |
| --- | --- | --- |
| Native allow and deny per tool | The compiled endpoint admits allowed and escalated tools by exact name and never a denied one (`test_openshell_agent_mcp.py`); the harness test's fake OpenShell applies those rules, and a denied tool is refused before the middleware | OpenShell enforcing them in this sandbox. The same endpoint form already runs at the proxy's fence (labs 13 and 39), and OpenShell's source runs the middleware chain after an mcp endpoint's rules admit a request |
| Escalation as deny-until-approved within 30 s | An escalated call is a permission request keyed as the proxy keys it (project, caller, revision, server, method, tool, parameters' digest), refused at once; retries wait on it; after `approve` the next retry passes and is recorded `approved`; a denial refuses. Nothing is held open, so an evaluation takes milliseconds (at most 0.25 s for the lock and 0.25 s for the store) | The run service's card in the workspace: the workspace reads the proxy's requests, not the middleware's |
| Argument rules | `mcp.proxy.max_argument_bytes`, parameters MCP does not define, task-augmented requests, duplicate keys and placeholders in bodies are refused, as in the proxy | Rules on argument values are built since (D96, A3): the library reads them from the body for the middleware as for the proxy; native rules cannot match arguments |
| Records with project, session and run | Every `mcp-call`, `escalation`, `approved` and `alert` record has the project (from the compiled config), the sandbox (from OpenShell's authenticated call) and the session and run (from the headers), never the arguments | The server's outcome and answer size: the middleware sees requests, and only listing answers |
| SSE passing through | Responses other than listings are skipped at preflight; a listing's event stream is passed on unit by unit, unchanged (`test_mcp.py`); the harness reads JSON and event-stream answers | OpenShell's relay of a real server's stream with the response binding declared |
| Tool-list drift | A complete listing, over pages, JSON or event stream, is compared with its pin; a drift is a finding and an `mcp-tools-drift` record, and blocks nothing | Built since (A3): pins come from the server's checked-in classification, with each tool's digest, and a drift is a `tool_list_drift` finding ([classification files](../MCP-CONNECTORS.md#classification-files)) |

## How holds behave

The middleware cannot hold a request until a person answers: one evaluation has at most 30 seconds, and OpenShell's default is 500 ms. It refuses instead, as it does for HTTP, and the harness calls again, as it does with the proxy (every 3 s for a minute, then every 10). The same arguments make the same request, since the client sorts keys and the key holds the digest of the parameters as MCP defines them; `_meta`, which passes to the server on this path, is not part of it.

OpenShell returns to the sandbox only the middleware's reason code, never its other words, so a refusal that concerns a request names it in the code: `approval_required_r12`, `approval_denied_r12`, `escalation_timed_out_r12` (`reasons.with_request`). Approving with `sandbox-science-policy-middleware approve --request 12` works for these requests as for network ones, with the scopes `call`, `tool` and `server`. The session and run are attribution only: they are in no key, so an approval made for one session covers the same call from another, as through the proxy.

Holds therefore fit.

## What the proxy still does that this path does not

- **Listings without denied tools** (lab 39, B5). The agent is offered `portal_*` tools it can never call. A JSON listing could be rewritten by response inspection; an event stream comes in 64 KiB pieces that the middleware may not carry across, so not reliably.
- **Binary results.** The proxy withholds images, audio and blobs; here they reach the agent's context as base64. D98 has them become artifact references through response inspection or control, which is not built.
- **Only advertised tools.** The proxy refuses a tool missing from the server's latest listing (`tool_not_advertised`); here the native rules and the policy decide by name alone.
- **Its own bytes upstream.** The proxy re-serializes what it decided; here the server gets the client's bytes, which the middleware has read strictly.
- **Answers.** The proxy bounds answers (`max_response_bytes`), answers a server's `ping` and refuses its other requests in a stream, and records each call's outcome. Here the harness bounds answers at 8 MiB and declares no capabilities, which the middleware requires.
- **Callers.** The proxy tells callers apart by token and applies `worker_access` and `worker_tools` to worker code. This path covers the agent runtime only; research sandboxes still cannot reach MCP hosts.
- **Credentials.** The provider is attached to the agent runtime's sandbox, whose processes hold placeholders, never values; the compiled endpoint admits only the interpreter named as its binary.
- **A server's 403.** A server can answer with a body that reads as OpenShell's denial, which nothing marks as OpenShell's own, and name any request number. `DirectClient` trusts a refusal that names a request only when its `RequestCheck` confirms it over the permission request records (`Approvals.confirms`): the request exists, stands as the code says, and is this call's by project, server, method, tool and parameters' digest. Anything else is the server's error (`server_error`, the number kept as `unconfirmed_request`): no card, no wait. The check needs those records, which only the tests give it so far (recommendation 2). Through the proxy the same holds for `fence_denied`.
- **The tool index** is the harness's already (`Connectors`), as D98 has it.

## Recommendation

Holds fit, and each proof passes offline, so by D98's rule the proxy should retire, not stay slimmed. Retire it only after:

1. a babbage lab runs `DirectClient` in an agent runtime sandbox against the attack-catalog server through a real OpenShell 0.1.5 gateway, showing the native rules, the middleware on an mcp endpoint, a reason code with its request number reaching the client, the response binding over a real event stream, and placeholders substituted;
2. the workspace reads and answers the middleware's MCP requests, its remembered approvals reach the middleware, and the harness's `RequestCheck` reads the same requests;
3. listings lose denied tools and binary results are withheld or become artifact references, by response inspection or in the harness.

Until then the proxy runs as built and the prototype is unwired.
