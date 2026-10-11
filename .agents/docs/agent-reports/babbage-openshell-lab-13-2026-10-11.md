# OpenShell v0.1.3 lab 13 on babbage, 2026-10-11: the MCP proxy in its own sandbox

This lab ran the MCP proxy (`packages/mcp-proxy`, D75) in its own OpenShell VM sandbox for the first time, under the policy `compile_mcp_proxy()` writes, then tore everything down. Callers reached it from the host and from an orchestrator sandbox. Like the earlier labs, this report records observed behaviour.

## What ran where

| | |
| --- | --- |
| Host | babbage: AMD Ryzen AI Max+ 395, Ubuntu 26.04.1 LTS, kernel 7.0.0-38-generic |
| Runtime | OpenShell 0.1.3, VM driver (KVM); sandboxes with 4 vCPUs, 4 GiB of memory and an 8 GiB overlay disk |
| Proxy sandbox | `ss-lab13-proxy`, from `sandbox-science/orchestrator:174e72e` (Python 3.14.8, aiohttp and pydantic). The proxy, the policy library, the event store and the pure-Python part of PyYAML were unpacked from wheels into `/sandbox/site`. The proxy listened on `127.0.0.1:8080` inside, and `openshell forward service --target-port 8080 --local 127.0.0.1:18080` exposed it on the host |
| Proxy policy | `compile_mcp_proxy()` for two servers, with the binary `/opt/sandbox-science/bin/python3.14`, plus a filesystem section added by hand (finding 3) |
| Servers | **deepwiki**: DeepWiki's public server, `https://mcp.deepwiki.com/mcp`, without credentials. **lab-echo**: a fake Streamable HTTP server on the host's Docker bridge address, `http://172.17.0.1:8095/mcp`, which logged every request and answered `echo`, `whoami` (did the expected token arrive?) and `ping_me` (a server-to-client `ping` before the result) |
| Credential | Provider `lab-echo` from a profile without endpoints, holding a synthetic token (`test-secret-lab13-mcp-…`), bound to lab-echo's endpoint by the compiled `credential_binding` |
| Callers | A script on the host, through the forward, and the same script in an orchestrator sandbox (`ss-lab13-orch`), whose policy admitted `host.openshell.internal:18080` beside the worker API |

The project policy requires `https` for MCP servers, and the fake server had no certificate OpenShell would trust (finding 5). For lab-echo only, a wrapper around the proxy, and the compile script, replaced its URL after the policy loaded: `https://lab-echo.lab.test/mcp` became `http://172.17.0.1:8095/mcp`. Everything else was the proxy's own code.

## Results

| Check | Result |
| --- | --- |
| The credential in the proxy's environment | `LAB_ECHO_TOKEN=openshell:resolve:env:v4803…_LAB_ECHO_TOKEN`: a placeholder only |
| Substitution | The fake server received the synthetic token in `Authorization` on every request, so OpenShell substituted the placeholder at the bound endpoint. `whoami` answered "the expected token arrived" |
| `MCP-Protocol-Version` at the fence | Every request after `initialize` carried `2025-11-25`; `initialize` carried none, and OpenShell admitted it |
| Replies to server requests | lab-echo sent a `ping` request in its event stream, and the proxy answered it with a POST carrying a JSON-RPC response. OpenShell's MCP inspection admitted it (`rule_methods=-`), and the server got the reply |
| What went upstream | Only the proxy's headers: `Content-Type`, `Accept`, `Accept-Encoding: identity`, `User-Agent: sandbox-science-mcp-proxy/0.1`, `MCP-Protocol-Version`, `Mcp-Session-Id`, `Authorization` |
| The proxy's refusals | `escalated`: `approval_required`, recorded as permission request 21. `forbidden`: `policy_denied`. An unlisted name: `tool_not_advertised`. None reached the server |
| An approved escalation | After `approve --request 21`, the proxy sent the call and OpenShell refused it: "MCP tool call has no matching allow rule" (403), as documented. The proxy reported `upstream_error` (finding 4) |
| TLS through the fence | Calls to DeepWiki worked: `initialize`, `tools/list` and `read_wiki_structure`. The proxy's default TLS context trusted OpenShell's sandbox CA through `SSL_CERT_FILE` |
| Names and addresses | Inside the sandbox, `mcp.deepwiki.com` resolved to `198.18.0.2`, and `pypi.org`, which no endpoint names, to `198.18.0.3`. OpenShell logged the real mapping: "Policy DNS mapped mcp.deepwiki.com resolved=32.187.211.51,44.241.197.126,54.244.1.94 synthetic=198.18.0.2" (finding 1) |
| Binary | OpenShell matched `/opt/sandbox-science/bin/python3.14`, the interpreter, not the script that runs the proxy |
| Stores | Policies, project bindings and the event store (`events.db`, 49 hash-chained records with the callers' attribution) lived under `/sandbox/proxy-state` on the sandbox's overlay disk. `bind`, `pending` and `approve` ran inside the sandbox through `openshell sandbox exec` |
| Stop and start | After `sandbox stop` and `start`, the files and all 49 records were there, and the placeholder was the same. The proxy process was not: it had run through `exec` (finding 6) |
| From an orchestrator sandbox | The same calls worked through `host.openshell.internal:18080`, with a `rest` endpoint and full access for the interpreter, the shape `compile_orchestrator()` now writes (finding 7) |

## Findings

1. **Inside OpenShell the proxy cannot see where a name leads.**
   - A sandbox resolves every name to a synthetic address from `198.18.0.0/15`. OpenShell's supervisor resolves the real one on the host.
   - The proxy checks the addresses it connects to against the hard floor and the pool ceiling, so there it checked only the synthetic address. A server name that resolved to a metadata, administration or ceiling-denied address would have passed.
   - The compiler said the opposite: "address-based floor and ceiling entries are applied by the MCP proxy".
   - **Fixed:** `compile_mcp_proxy()` now gives each server named by host name `allowed_ips`: the public ranges, less the floor's and the ceiling's addresses (`mcp_server_ranges()`). OpenShell then refuses a name that resolves anywhere else. A server named by address keeps that address alone. The proxy's own check still applies outside OpenShell.
2. **Credentials, versions and server requests work as built.** The placeholder is substituted in headers, the version header satisfies the fence, and the proxy's replies to server requests pass OpenShell's inspection. None of these needed a change.
3. **The proxy sandbox's policy needs its image's prefix.**
   - Like the orchestrator's (lab 12), it needs `/opt/sandbox-science` read-only, but `McpProxyTarget` could not express it, and the lab added it by hand.
   - **Fixed:** the target takes a `filesystem` setting.
   - The example targets file now names the interpreter, `/opt/sandbox-science/bin/python*`, as the binary.
4. **The proxy could not tell the fence's refusal from the server's.**
   - OpenShell answers a refused call with 403 and a JSON body naming the error (`policy_denied`), the policy and the layer.
   - **Fixed:** the proxy now reports it as `fence_denied`, so an operator knows to look at the compiled policy.
   - A server could send the same body, so the call still counts as sent (error -31002).
5. **Upstream TLS trusts the host's CAs, not the sandbox's.**
   - With the VM driver, `openshell-supervisor` runs on the host. It verifies servers against Mozilla's roots and the host's system bundle, which the lab did not change.
   - A private MCP server with a CA of its own would need that CA in the host's bundle.
   - Hence the lab's URL wrapper for the fake server, which had no such certificate.
6. **The proxy needs an image of its own.**
   - Run through `exec`, it stops with the sandbox and does not come back.
   - An image that installs the proxy can run it as the sandbox's command, so that `sandbox start` brings it back.
   - Its operators reach the stores only through `openshell sandbox exec`, and the event store's records stay in the VM until something exports them (backlog).
7. **The orchestrator reaches the proxy like the worker API.** `compile_orchestrator()` now takes an `mcp_proxy` endpoint, compiled the same way as the worker API's.
8. **Smaller notes.**
   - `openshell sandbox upload` of a directory to `/sandbox/site` created `/sandbox/site/site`; upload to the parent instead.
   - `forward service` exposes a sandbox's loopback service on the host; other sandboxes then reach it as `host.openshell.internal`.
   - babbage's resolver drops wildcard-DNS answers that point at private addresses (`*.172-17-0-1.sslip.io` resolved to nothing), so the fake server was named by its address.

## Cleanup

- **Removed:**
  - both sandboxes, the provider and the profile;
  - the units: the forward, the proxy's `exec`, the fake server, the gateway, the middleware and the cache;
  - the lab directory (1.0 GB).
- **Checked afterwards:**
  - no lab processes;
  - the lab ports free (8080, 8090, 8095, 8100, 18080, 50051, 17670);
  - no OpenShell paths under `~/.config`, `~/.local/state` or `~/.local/share`;
  - no driver socket directories in `/tmp`.
- **Kept:** the fake server's log, with the token replaced by `<LAB_ECHO_TOKEN>`, in the session scratchpad; and the images in the local Docker store.
