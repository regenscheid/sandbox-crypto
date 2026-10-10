# Project policy file

Date: 2026-10-10. Proposed (D62); the evaluation details below are proposed in D68 and implemented by the `sandbox_science.policy` library ([packages/policy](../packages/policy)). The user wants permissions to stay policy-based and to be defined in a YAML file that is quite permissive by default, with a configurable timeout after which unanswered requests fall back to deny. This document defines the semantics. The example is [examples/policy.yaml](../examples/policy.yaml); the library's tests load it, so it stays valid.

## Layers and precedence

Three layers combine for every operation. A lower layer can only narrow what the layer above allows.

| Layer | Owner | Editable by | Purpose |
| --- | --- | --- | --- |
| 1. Hard floor | Platform code | Nobody at runtime | Rules that keep the sandbox a sandbox, whatever the policy says |
| 2. Pool ceiling (D49) | Each worker pool's local configuration | Local user action only | Maximum resources, destinations, mounts and data classes for that pool |
| 3. Project policy (this file) | The project | The user, through the GUI or file, with revision history | Day-to-day research permissions |

**Hard floor (always deny):**

- reaching gateway, monitor, policy or engine administration APIs;
- cloud metadata endpoints, and link-local, loopback and unspecified addresses (including `localhost` and `*.localhost`), however the address is written;
- raw secret material (credentials exist in sandboxes only as provider placeholders);
- writes to policy, monitor configuration or the event log;
- host-path mounts beyond declared volumes;
- privilege escalation, container-engine sockets, kernel module loading;
- running research code in the orchestrator.

The library checks the network items for every operation, including the URLs of declared MCP servers. The hosts of declared MCP servers are themselves on the floor for generic egress, whatever the path (a server declared by IP address also matches any name that resolves to it), because servers often offer other transports (such as `/sse`) beside the declared one: they are reachable only through the MCP proxy (D67). Server URLs must have a plain path, without `%`, `;`, dot segments or a query. The remaining items are enforced by the platform and OpenShell (see [mapping to enforcement](#mapping-to-enforcement)).

Agents can never change any layer. They can *propose* a project-policy change through the OpenShell policy advisor path (auto-approval off), and that proposal is an ordinary escalation.

The **pool ceiling** can list allowed and denied hosts, the MCP servers the pool may reach, and resource caps. A destination matching `deny_hosts` (wildcards included) is denied. When `allow_hosts` is present, a destination it does not match is denied, so `allow_hosts: []` permits none; omitting `allow_hosts` leaves destinations unrestricted at this layer. `mcp_servers` lists the MCP server names the pool may reach; an empty list permits none. A ceiling denial applies whatever the project policy's action would be. A key written without a value (for example `allow_hosts:` with every entry commented out) is an error rather than "unrestricted". A project's resource requests above a cap are reduced to the cap and reported.

The built-in floor covers loopback, link-local and unspecified addresses (including `0.0.0.0/8`); the AWS, Azure, GCP, Oracle, Alibaba and Tencent metadata endpoints, over IPv4 and IPv6; loopback aliases (`localhost`, `*.localhost`, `localhost.localdomain`, `ip6-localhost`); container-host names (`host.docker.internal`, `host.containers.internal` and similar); and the Kubernetes API service names. It recognizes these addresses inside IPv4-mapped, translated, NAT64, 6to4, Teredo and IPv4-compatible IPv6 forms. Each deployment adds its own administration endpoints.

## Actions

| Action | Effect |
| --- | --- |
| `allow` | Proceed; logged |
| `alert` | Proceed; record a finding and notify |
| `escalate` | Hold only the affected operation; create a durable permission request; resume or safely retry on approval, block on denial or timeout |
| `deny` | Block; record the rule and reason |

Restrictiveness, from most to least: `deny` > `escalate` > `alert` > `allow`.

## Evaluation

The hard floor and the pool ceiling are applied first and cannot be overridden. Within the project policy, evaluation has two steps.

### 1. Classify the operation

Protocol-aware matchers first decide what a request *is*, before any generic HTTP rule sees it:

| Operation class | Recognized by | Governed by |
| --- | --- | --- |
| `package` | Requests to the package mirror | `packages` |
| `git` | Git smart-HTTP service (`git-upload-pack` is fetch, `git-receive-pack` is push), whatever the HTTP method | `network.rules` with `operation: git` |
| `mcp` | MCP JSON-RPC to the MCP proxy, classified by JSON-RPC method and tool name | `mcp.servers` (the MCP proxy) |
| `http` | Everything else | `network.rules` with `operation: http` |

Generic `http` method rules never apply to `git`, `mcp` or `package` operations. This matters because both git fetch and MCP's Streamable HTTP transport send reads as `POST`. Without classification, a rule escalating every `POST` would also escalate those reads.

Git smart-HTTP is recognized from the request: `GET <repo>/info/refs?service=git-upload-pack` or `?service=git-receive-pack`, and `POST <repo>/git-upload-pack` or `POST <repo>/git-receive-pack` carrying a body and exactly git's own content type, without parameters (`application/x-git-upload-pack-request` or `application/x-git-receive-pack-request`). Any other POST is ordinary `http`, so a REST call whose path happens to end in `/git-upload-pack`, such as a "create file" API, stays a write. The discovery request of a push (`service=git-receive-pack`) already counts as a push. Dumb-HTTP git reads, and any other method, path or `service` value at a git endpoint, are ordinary `http` operations. Package and MCP operations are classified by the package mirror and the MCP proxy, which are the only routes for that traffic. The egress proxy reports each request's Content-Type, whether it carries at least one body byte, and any Upgrade header, and passes the IP addresses it is about to connect to (`resolved`). The hard floor (administration endpoints included), the pool ceiling's `deny_hosts` and the public/non-public rule apply to every resolved address, in every form an address can take (`::ffff:10.0.0.5` is `10.0.0.5`). Rules that name an IP address match requests to that literal address; to block an address whatever name reaches it, list it in the pool ceiling's `deny_hosts`. For the same reason, list administration endpoints in `admin_hosts` by address as well as by name.

A request that cannot be classified unambiguously is **denied**. That includes:

- a method not written in upper case (methods are case-sensitive), a protocol upgrade such as WebSocket, and a body on `GET`, `HEAD` or `TRACE`;
- user information in the URL (`user@host`, even empty) and a malformed authority;
- host names that are not ASCII (use the IDNA form), host names whose last label is all digits or `0x` hex without being a canonical IP address (`2130706433`, `0x7f.1`, `127.1`), brackets around anything but an IPv6 address, and IPv6 literals with a zone ID or an embedded IPv4 address (write the IPv4 address);
- invalid percent-encoding; an encoded `/`, `\`, `.`, `%` or NUL in the path; a decoded `?` or `#`; dot or empty path segments; a segment that begins with a space or ends with `.` or a space (some servers trim them); a soft hyphen, characters that Unicode normalization turns into path punctuation (a fullwidth `.` or `/`), or characters that Windows best-fit conversion turns into `/`, `\` or `:` (such as U+2215 division slash), and `::$` (Windows alternate data streams); and `;` path parameters (servers disagree on them);
- a repeated git `service` parameter.

The path checks allow a trailing slash. The query string is neither checked nor matched, except for the git `service` parameter.

### 2. Pick the rule

Every network rule names at least one host. A rule matches an operation of its class when every condition it states holds:

- **hosts**: an exact name or IP address matches only itself (case-insensitive; one trailing dot is ignored). `*.example.org` matches names with at least one more label (`a.example.org`, `a.b.example.org`), not `example.org`. `*` matches any host.
- **ports**: a rule without `ports` matches only the scheme's default port (443 for `https`, 80 for `http`).
- **paths**: prefixes compared segment by segment on the decoded path. `/v1/search` matches `/v1/search` and `/v1/search/x`, not `/v1/searchx`. The query string is ignored. Paths are case-sensitive.
- **methods** (`http`) or **git_service** (`git`): the request's method or service must be listed.

Among the matching rules the **most specific rule wins**, and ties go to the **most restrictive action**. When several rules tie, the `limits` of every tied rule apply, and an escalation takes the shortest tied timeout. Specificity is compared in this order:

1. host: exact host > wildcard host (`*.example.org`) > `*`;
2. between wildcard hosts, more labels after `*.` is more specific;
3. a longer matching path prefix (counted in segments) is more specific than a shorter one or none;
4. a rule that names methods (or a git service) is more specific than one that does not, however many it names.

Ports are a condition only; they do not make a rule more specific. When a rule lists several hosts or paths, the most specific one that matched counts.

So a narrow `escalate` or `deny` still overrides a broad `allow`, which keeps broad allows safe to write. A narrow `allow`, such as a read-only `POST` search API on one host, can override a broad "escalate all writes" rule. Operations that match no rule take `defaults.action`.

**Wildcards and defaults never grant non-public destinations.** Non-public means an IP address outside the public unicast space (private, shared/CGNAT, multicast, reserved and documentation ranges, IPv6 outside `2000::/3`, and IPv4 addresses embedded in IPv6), a single-label name (resolvers complete it with local search domains), or a name under a special-use or private-use suffix: `.local`, `.localdomain`, `.internal`, `.home.arpa`, `.lan`, `.home`, `.corp`, `.svc`, `.test`, `.example` or `.invalid`. When the winning rule names such a destination only through a wildcard (`*` or `*.suffix`), or no rule matches, an `allow` or `alert` outcome becomes `deny`; `escalate` and `deny` outcomes are unchanged. Allowing a non-public destination takes a rule that names it exactly. When the pool ceiling lists allowed hosts, it must name the destination exactly as well.

### Rule limits

`limits.max_url_bytes` bounds the length of the full URL, on `http` and `git` rules. When the winning action is `allow` or `alert` and the URL is longer than any tied rule's limit, the request is denied; it does not fall through to another rule. An escalation whose URL exceeds a limit is denied when it times out. This bounds how much data a read can carry out in its URL (threat T7).

### MCP calls

- The server must be declared under `mcp.servers`; calls to an undeclared server are denied. Server URLs must use `https` and are subject to the hard floor.
- `tool_prefix` declares a namespace prefix the server puts on every tool name, as a portal does (`crypto_research_mcp-`). Tool patterns then match the name without the prefix, and a tool without it is denied.
- Calls from worker code are denied unless `mcp.proxy.worker_access` is `true`. When `worker_tools` is a list, a worker's `tools/call` must also match one of its patterns.
- JSON-RPC methods: the protocol and discovery methods `initialize`, `ping`, `notifications/initialized`, `notifications/cancelled`, `notifications/progress`, `notifications/roots/list_changed`, `tools/list`, `resources/list`, `resources/templates/list`, `resources/read`, `resources/subscribe`, `resources/unsubscribe`, `prompts/list`, `prompts/get`, `completion/complete` and `logging/setLevel` are allowed. `tools/call` is decided by the tool rules. Every other method, including extension and task methods, is denied, and so are JSON-RPC batches.
- Tool patterns may contain letters, digits, `_`, `.`, `/`, `-` and `*`, which is the only wildcard; a pattern must match the whole tool name. Method and tool names are case-sensitive. An exact name (without `*`) takes precedence over patterns. Otherwise the most restrictive list among the matching entries wins (`deny` > `escalate` > `allow`). A tool that matches nothing takes `defaults.action`. The same pattern may not appear in two lists.

Tool names are matched as text, so a broad pattern can admit a tool that writes although its name looks like a read. Without `tool_prefix`, `*search*` matched every crypto-research-mcp tool, because the portal prefix contains "research"; it also matches `firecrawl_search_feedback`, which submits feedback. **Allow tools by exact name, and use patterns only to escalate or deny**: any allow pattern can admit a tool a later revision adds or a compound name such as `get_or_create_note`. Deny a server's configuration namespace as a whole (`portal_*`) with exact exceptions, and check the lists against the pinned revision's tool list; the library's tests do this for crypto-research-mcp, including a made-up tool, which must ask first. The MCP proxy must forward only tool names the server advertises, exactly as advertised, so that a server that folds case cannot be reached through a variant spelling.

### Packages

A conda package from a channel that `packages.conda.channels` does not list is denied. An ecosystem without a `packages` entry takes `defaults.action`. PyPI names are compared in normalized form (PEP 503).

### Escalation timeout

`escalation.timeout` sets how long a request waits for an answer (default `30m`). When it expires, the request is denied: the policy-wide `escalation.on_timeout` must be `deny`. A rule can override the timeout for itself. A rule that only reads, meaning an `http` rule whose `methods` are all `GET` or `HEAD`, or a `git` rule for `upload-pack`, may also set `on_timeout: allow`. A timed-out request under such a rule proceeds as an `alert`, with a recorded finding, never silently. A request that carries an injected credential, or whose URL exceeds a limit, is denied on timeout regardless. When escalate rules tie, the request proceeds as an alert only if every one of them permits it. MCP tool escalations use the policy-wide setting.

Other work continues while a request waits (`hold_scope: operation`). A denied or timed-out request is not retried automatically.

### Policy file format

Policy and ceiling files use a strict YAML dialect, so that what a reviewer reads is what is enforced. Explicit tags, aliases, merge keys and duplicate keys are errors. Mapping keys are text, and every key has a value: a key written without one (for example with its value commented out) is an error, never "off". Numbers are plain decimals (YAML 1.1 would read `0443` as 291 and `1:20` as 80), booleans are `true` or `false` (not `yes` or `on`), quoted values are not converted (`"yes"` is not true), and dates must be quoted. Double-quoted strings may not use backslash escapes. So that nothing hides beyond the edge of an editor or diff, lines are at most 160 columns wide (wide characters count twice), a run of four or more blanks inside a line is allowed only before a comment, and whitespace other than ASCII space and tab is rejected. Numbers must be finite. Invisible and bidirectional formatting characters, Unicode line and paragraph separators and stray carriage returns are rejected; a leading byte-order mark and CRLF line ends are accepted. Path prefixes are printable ASCII. `memory` and `workspace_disk` must be positive.

### Monitors

Threshold rules, such as egress volume, live in `monitors`, not in `network.rules`. They never decide whether an operation may proceed; they add findings when usage crosses a threshold. A threshold written as an access rule could never fire: every access rule that matches the same traffic outranks a rule that names no host or method.

## What the permissive default allows

The shipped example is "quite allowing", as requested:

- **Freely allowed:** package installs from conda-forge and PyPI through the mirror; read-only HTTP (`GET`/`HEAD`) to public hosts, with a URL length limit; git clone/fetch from GitHub, GitLab and Codeberg (classified as `git`, so its `POST` requests are not caught by the HTTP write rule); read-type tools on crypto-research-mcp (classified as `mcp`); writes to the project's shared data, with conflict checks.
- **Asks first:** any external `http` write (`POST`/`PUT`/`PATCH`/`DELETE`) not covered by a more specific allow, git push, MCP tools that change state or start paid work (crawls, agent runs, monitors, refreshes, feedback, page interaction), and anything unmatched, including non-public hosts and non-default ports.
- **Never:** MCP server configuration tools, plus the hard floor.
- **Alerts:** unusually large egress volume (a monitor).

This deliberately relaxes the earlier review proposal (D44) that unknown destinations should never be reachable without approval. Reads are broadly allowed. The primary threat is agent overreach, which mostly shows up as side effects, and those still escalate. The remaining cost is that read requests can carry small amounts of data out in URLs; the URL limit and logging bound it.

## Mapping to enforcement

| Policy element | Enforced by |
| --- | --- |
| Hard floor | Platform code, OpenShell filesystem/process policy, network policy with no override path |
| `network.rules` with exact or `*.suffix` hosts | OpenShell network policy endpoints with L7 method/path rules. The compiler sets `enforcement: enforce` explicitly, because OpenShell's default is audit, which only logs. `*.suffix` becomes OpenShell's `**.suffix` (one or more labels); OpenShell needs at least three labels in a wildcard host, so shorter suffixes go through the egress proxy |
| Rules with `hosts: ["*"]` | The project egress proxy, which applies this library's decisions. OpenShell cannot express "any host" as an endpoint, so sandboxes reach generic HTTP only through the proxy, which OpenShell permits as an exact host. OpenShell's `upstreamProxy` is a gateway setting that only chains TLS tunnels, so it cannot carry these rules |
| `mcp.servers.*.tools` | The MCP proxy, re-enforced by OpenShell `protocol: mcp` endpoints with tool-name rules; `escalate` compiles to deny-plus-pending-request. OpenShell needs the protocol methods allowed explicitly and controls tools by name only |
| `mcp.servers.*.auth` | OAuth via the connector auth service, or bearer/custom headers via provider placeholders placed by the MCP proxy ([MCP connectors](MCP-CONNECTORS.md), D66, D67). Literal secrets in header values are rejected |
| `credentials.providers` | OpenShell providers bound to their endpoints (D57) |
| `escalate` | Deny in the enforcement layer, plus a durable request in the shared permission workflow; resume through a scoped decision (MONITORING.md) |
| `alert` | Allow, plus a monitor finding |
| `monitors` | Monitor findings from proxy and gateway usage counters |
| `resources` | Runtime limits, capped by the pool ceiling; model budget through the LiteLLM virtual key (D61) |
| `packages` | Mirror allowlist and read-only rootfs (D44, D48) |

Host checks must also apply to the address a name resolves to. The egress proxy resolves names itself (proxy-side DNS) and re-checks the resolved address against the hard floor and the wildcard rule, so a public-looking name that resolves to a private or metadata address is treated like that address. OpenShell does the same for its endpoints: it always blocks loopback, link-local and unspecified addresses, blocks private ranges unless an exact host, IP literal or `allowed_ips` entry names them, and dials the addresses it validated, which closes the DNS-rebinding window.

OpenShell combines rules differently from this file: it unions allows and lets any matching deny win, with no specificity ordering. The compiler therefore emits allow rules for the regions this policy allows, and deny rules only to carve exceptions out of broader allows. Where a narrow allow sits inside a narrower deny inside a broad allow, OpenShell enforces the stricter result; the compiler reports it.

The policy compiler is part of infrastructure track P. It reports any rule it cannot enforce on the selected backend rather than silently dropping it, and records the effective policy revision (the SHA-256 digest of the policy file) with every execution.

## Residual risks

Three adversarial review rounds of the library (2026-10-10, pinned by `test_review_regressions.py`) left these for the components around it:

- Paths are compared case-sensitively, so a path `deny` can be bypassed on a server that ignores case. Prefer narrow allows to path denies.
- An allowed git fetch sends a request body to the git host. The egress proxy should check that upload-pack bodies are pkt-line requests and bound their size, especially under rules that allow fetches from many hosts.
- MCP tools that fetch URLs take the URL as an argument, which `max_url_bytes` does not see. The MCP proxy should bound argument sizes.
- The library checks the resolved addresses the egress proxy passes it; a proxy that skips them loses those checks.
- The MCP host floor matches the declared host, so another name for a server declared by name (an alias, or the same app under another domain) is not recognized. MCP credentials stay in the MCP proxy, so such a request carries none, and writes still escalate as generic `http`. The MCP proxy must verify the upstream TLS certificate against the declared host.
- Some servers interpret paths in ways the classifier does not model, such as case-insensitive matching or fullwidth letters on Windows; this is why path denies are weak.

## Open items

- Compiling to OpenShell v0.1.3: the facts above come from the [OpenShell policy investigation](../.agents/docs/agent-reports/openshell-policy-v0.1.3.md); the compiler itself is not written yet.
- MCP tool-name patterns depend on the actual tool names the server exposes per revision; pin the server revision in the policy.
- How GUI edits and file edits of the policy merge (proposed: the file is the source of truth, the GUI edits it, every change is a revision). Widening the policy is a re-authentication action (D54).
- Data-class limits in the pool ceiling (D50) are not yet modeled.
