# NVIDIA OpenShell v0.1.3: policy schema and enforcement semantics

Captured 2026-10-10 by a research subagent for the policy library (track P). Source inspection only: OpenShell was not installed or run.

Research for the sandbox-crypto project-level policy library (future compile target: OpenShell policy).

## 0. Provenance, method, conventions

- Repo `NVIDIA/OpenShell`, tag `v0.1.3`. `gh api repos/NVIDIA/OpenShell/git/ref/tags/v0.1.3` returns commit
  `e1f3c82caa3ed3b65de22889ae7ef32a774878ef` (commit date 2026-10-09, "fix(deps): update noyalib and preserve policy null rejection (#4348)").
  Every path below is read at that tag; line numbers refer to the file at the tag.
- Read-only: `gh api .../contents/<path>?ref=v0.1.3` only (files cached as text in the session scratchpad). Nothing cloned, built or run.
  `gh search code` (default branch) was used only to locate files; every hit was re-read at the tag.
- One dependency was read outside the OpenShell repo: `regorus` 0.9.1 (pinned at `Cargo.lock:6393-6394`), file `src/builtins/glob.rs` at tag
  `regorus-v0.9.1` of `microsoft/regorus`, because the glob semantics of the embedded Rego engine decide host/path/tool matching.
  `tower-mcp-types` (MCP method registry) was NOT read; its method sets are taken from the OpenShell docs.
- Evidence labels: **[doc]** statement in docs at the tag; **[code]** read directly in source; **[test]** asserted by a unit test in the repo;
  **[inference]** my reading of the code, not exercised by a test I could point to.
- Path shorthand used below:
  `rego` = `crates/openshell-supervisor-network/data/sandbox-policy.rego`; `net/` = `crates/openshell-supervisor-network/src/`;
  `schema-rs` = `crates/openshell-policy-schema/src/lib.rs`; `pol/` = `crates/openshell-policy/src/` (`pol/lib.rs`, `pol/ambiguity.rs`, `pol/compose.rs`, `pol/l7_validate.rs`);
  `core/` = `crates/openshell-core/src/`; `srv/` = `crates/openshell-server/src/`.
  Docs (all under `docs/`): `schema.mdx`, `network-rules.mdx`, `manage-policies.mdx`, `overview.mdx`, `default-policy.mdx`, `advisor.mdx`, `prover.mdx` = `how-it-works/policies/<name>`;
  `profiles.mdx` = `how-it-works/providers/profiles.mdx`; `providers-overview.mdx` = `how-it-works/providers/overview.mdx`; `configuration.mdx` = `how-it-works/gateways/configuration.mdx`;
  `runtimes.mdx` = `how-it-works/sandboxes/runtimes.mdx`; `logging.mdx` = `observability/logging.mdx`; `best-practices.mdx` = `security/best-practices.mdx`;
  `kubernetes/setup.mdx`, `upgrade/0-1-0.mdx`, `about/architecture.mdx` are written with their `docs/` subpath.

### Pipeline in one paragraph (context for everything below)

Authored YAML (`version: 1`) is parsed by the strict `openshell-policy-schema` crate, converted to the protobuf `SandboxPolicy`
(`proto/sandbox.proto:21`), stored by the gateway as revisions, polled by the supervisor (default every 10 s,
`crates/openshell-supervisor/src/lib.rs:1172-1175`), converted to JSON data and loaded into an embedded Rego engine
(`regorus`; baked rules in `rego`, loaded at `net/opa.rs:40`). Access presets are expanded into explicit rules before Rego sees them
(`net/l7/mod.rs:1765-1810`). The supervisor (trusted side) terminates TLS for inspected endpoints, evaluates policy per connection and
per request, resolves DNS and dials upstream. HTTP inspection is HTTP/1.1 only (`net/l7/tls.rs:229,285` set ALPN to `http/1.1`;
h2c is refused, `net/l7/rest.rs:84-90`). [code]

---

## 1. Network policy endpoint fields

Authoritative field list for an endpoint is the serde struct `NetworkEndpoint` (`schema-rs:256-331`, `deny_unknown_fields`); see Appendix A.

### 1.1 Host matching

- **Exact host** (no `*` in the string): `lower(endpoint.host) == lower(network.host)` (`rego:108-114`). Case-insensitive. [code]
- **Wildcard host** (string contains `*`): `glob.match(lower(endpoint.host), ["."], lower(network.host))` (`rego:119-125`). [code]
  The engine is regorus 0.9.1. Its `glob.match` rewrites each delimiter `.` to `/./`, uses `globset` with `literal_separator(true)`,
  and **defaults the delimiter to `.` when the delimiter array is empty** (`regorus/src/builtins/glob.rs:81-106`). [code, dependency]
- **How many labels a wildcard covers** [doc + code + test]:
  - `*` stays inside one DNS label. `*.example.com` matches `api.example.com`, not `example.com`, not `deep.sub.example.com`
    (`net/opa.rs:10329-10400`; docs `schema.mdx:578-590`). `*` may be partial in the first label (`*-api.example.com`, test `net/opa.rs:12236-12286`).
  - `**` is allowed only as the entire first label and consumes one or more labels: `**.example.com` matches `api.example.com` and
    `a.b.example.com`, not `example.com` (`core/host_pattern.rs:8-13,154-158`; parity table `net/opa.rs:12288-12337`; docs `schema.mdx:131-133,578-586`).
  - A mid-host `*` must be a whole label (`api.*.example.com`; test uses `*.s3.*.amazonaws.com`, `net/opa.rs:10534`).
- **Load-time validation** (`net/l7/mod.rs:640-683`, duplicated for typed policy at `pol/lib.rs:1513-1526,1687-1707`): reject `*` or `**` as the
  whole host; reject `**` anywhere except the entire first label; reject a non-whole `*` in any label after the first; reject any wildcard host with
  two or fewer labels ("TLD wildcard", so `*.com` and `**.org` fail). Effective minimum: three labels.
- **Hostless endpoint**: `host` may be omitted only if `allowed_ips` is non-empty; it then matches any host name (including raw IP destinations)
  on the listed ports, and IP checks happen after DNS resolution (`rego:127-136,1070-1075`; `pol/lib.rs:1471-1478`; `schema.mdx:135-138`). Not allowed
  for `protocol: tcp` (`pol/lib.rs:1469-1473`) nor `protocol: mcp` (`net/l7/mod.rs:1255-1270`).
- **IP literals**: a literal IP in `host` is an exact string match (IPv6 written without brackets; the CONNECT parser strips brackets and one
  trailing dot, lowercases, `net/proxy.rs:6817-6839`). A literal IP host synthesizes an implicit `allowed_ips` of exactly that IP, so RFC 1918
  literals work; loopback, link-local and unspecified literals are skipped with a warning and stay blocked (`net/proxy.rs:4070-4086`; tests `net/proxy.rs:12639-12660,14155-14180`). [code/test]
  Explicit `protocol: tcp` endpoints must use DNS names; an IP literal is rejected (`pol/lib.rs:1480-1490`).
- **CIDRs**: not accepted in `host`. CIDRs (or bare IPs) go in `allowed_ips`, validated by `parse_allowed_ips` (`net/proxy.rs:4621-4680`): invalid entries and any
  entry overlapping the always-blocked ranges fail the load; a prefix shorter than /16 only emits a warning event (`net/proxy.rs:4601,4650-4670`).
  A CIDR written in `host` would be an exact-string host that no CONNECT can match [inference].
- Not in the docs but true in practice: `?` and `[..]` inside a label of a wildcard host work (the parity table includes `*.a?i.example.com`,
  `net/opa.rs:12323`), although `schema.mdx:584-585` says endpoint hosts accept only `*` and `**`. Validators check only star placement.
  A policy host with a trailing dot or `{a,b}` braces is not validated and would not behave as intended [inference].
- Scheme is not part of matching; the schema is TCP-only (`port` is "TCP port", `schema.mdx:127`).

### 1.2 Ports

- `port` (single) or `ports` (list). **Required**: at least one effective port or the policy is rejected (`PolicyViolation::MissingEndpointPort`,
  "must declare at least one port", `pol/lib.rs:1491-1502`). **No default** (no implicit 443/80). Range 1..=65535 (`pol/lib.rs:1503-1511`); YAML values
  over 65535 fail at parse because the field is `u16` (`schema-rs:264`, test `schema-rs:1212-1218`).
- Effective ports: a non-empty `ports` wins, else `port` if non-zero (`schema-rs:1107-1113`; proto comment `proto/sandbox.proto:145-147,168-171`).
- Docs say use `ports` instead of `port`, not with it (`schema.mdx:126-127`). In code, when both are present `ports` silently wins; the
  "port and ports are mutually exclusive" validator (`net/l7/mod.rs:1285-1300`) runs after `normalize_endpoint_ports` has already deleted scalar
  `port` (`net/opa.rs:1677` vs `1707`; typed path `net/opa.rs:2302-2311`), so it appears unreachable [inference]. Emit only one.
- `protocol: mcp` additionally requires `host` and a port (`net/l7/mod.rs:1255-1270`).

### 1.3 Protocols

- Documented values: `rest`, `websocket`, `graphql`, `mcp`, `json-rpc` (request inspection) and `tcp` (explicit L4) (`schema.mdx:155`).
  Omitted protocol = L4 only (still TLS-terminates and parses HTTP unless `tls: skip`, `network-rules.mdx:105-110`).
- **Undocumented**: `sql` is accepted by the validators (`pol/l7_validate.rs:94-115`, error text lists it, line 255) and by the proto comment
  (`proto/sandbox.proto:148-150`), but the relay is an unimplemented passthrough with a warning ("SQL provider is Phase 3", `net/l7/relay.rs:1062-1085`)
  and `enforcement: enforce` is rejected (`net/l7/mod.rs:1485-1488`; `pol/lib.rs:1630`). Rules may carry a `command` field (SQL) that is also undocumented
  (`schema-rs:394`). Do not emit `sql`.
- Protocol names are case-insensitive and normalized (`net/opa.rs:1727-1765`; `L7Protocol::parse` lowercases, `pol/l7_validate.rs:105-115`).
- `tcp`: requires a DNS hostname and port; rejects `path`, `enforcement`, `access`, `rules`, `deny_rules`, credential rewrite/signing, protocol options
  (`pol/l7_validate.rs:259-266`; `pol/lib.rs:1582-1612`; `schema.mdx:218-220`). Explicit `tcp` is the native non-HTTP form (databases, SMTP...); it must not overlap a
  non-`tcp` endpoint with different handling (`transparent_tcp_eligible`, `pol/ambiguity.rs:166-175`). Policy DNS itself (see 7.3) applies to every endpoint with a host and ports, not only `tcp`.
- `tls`: only `""` (auto-detect and terminate) or `skip` (raw relay, no inspection, no credential rewrite). `terminate` and `passthrough` are
  rejected (`pol/l7_validate.rs:71-75,192-208`; `proto/sandbox.proto:105-115`; upgrade note `docs/upgrade/0-1-0.mdx:103`).
- **`tls: skip` + a request protocol is only a warning** at load (`net/l7/mod.rs:1476-1481`, port 443 only), yet the rules are then not enforced
  because the stream is a raw tunnel (docs `profiles.mdx:239`: "shown as a raw tunnel ... even when it declares an L7 protocol and rules"). [code/doc]

### 1.4 `enforce` vs `audit`

- Values `enforce` | `audit` | omitted. **Omitted = `audit`** (`EnforcementMode` `#[default] Audit`, `net/l7/mod.rs:103-110`; `parse_l7_config`
  maps `""|"audit"` to Audit, `net/l7/mod.rs:297-330`; proto comment `proto/sandbox.proto:118-124`; docs `schema.mdx:157`).
- `enforce`: a request that fails the endpoint's rules gets `403` with a JSON `policy_denied` body and never reaches upstream (`logging.mdx:206-217`; `best-practices.mdx:105-110`).
  `audit`: violation is logged (decision `audit`, action Allowed) and the request is forwarded (`net/l7/relay.rs:1361-1367`, `2514-2519`).
- Audit applies only to allow/deny rule outcomes. Hard failures are denied even under audit: malformed or non-canonical requests, request-authority mismatch,
  JSON-RPC/MCP parse or profile errors, GraphQL parse errors, unsupported `Upgrade` on graphql/mcp/json-rpc endpoints (`net/l7/relay.rs:3022-3040`,
  `network-rules.mdx:124-126`, `schema.mdx:371-378`).
- At L4 (no protocol) there is nothing to audit: a connection with no matching rule is denied (`rego:18-20`).

---

## 2. L7 HTTP rules

### 2.1 Rule syntax

- YAML: `rules: [ { allow: { method, path, query } } ]` and `deny_rules: [ { method, path, query } ]` (deny entries are not wrapped in `allow`)
  (`schema.mdx:239-281`; structs `schema-rs:384-465`). `access` presets (`read-only` = GET/HEAD/OPTIONS; `read-write` adds POST/PUT/PATCH, **not DELETE**; `full` = `*`)
  expand to `{method, path: "**"}` rules (`net/l7/mod.rs:1765-1810`; `schema-rs:1143-1151`). `access` and `rules` are mutually exclusive
  (`pol/l7_validate.rs:268-271`); `rules: []` and `deny_rules: []` are errors (`net/l7/mod.rs:1595-1604`); `deny_rules` need a `protocol` and a base allow set
  (`pol/l7_validate.rs:319-327`).
- **Method**: case-insensitive exact match, or `*`; additionally a **`GET` rule also matches `HEAD` requests** (RFC 9110), for allow and deny alike
  (`rego:761-773`; test `net/opa.rs:6207`). Non-standard method names are only a warning (`net/l7/mod.rs:1904-1911`: GET HEAD POST PUT DELETE PATCH OPTIONS `*`).
- **Path**: a glob over the canonicalized path with `/` as separator, **not** a prefix and **not** a regex (`rego:775-788`). `**` alone matches everything;
  `*` stays within a segment; a whole-segment `**` crosses segments and needs at least one more segment, so `/repos/**` does not match `/repos`
  (test `net/opa.rs:6178-6205`; docs `schema.mdx:578-595`); `?` and `[..]` classes work (docs `schema.mdx:583-585`). `{a,b}` alternation is supported by the
  underlying globset (`check_glob_syntax` only warns on unbalanced braces, `net/l7/mod.rs:600-638`) [inference]. A REST rule with no `path` is not
  rejected by any check I found and would simply never match [inference].
- **Query** (`query: { name: glob | {any: [globs]} }`): parameter names case-sensitive; values percent-decoded and `+` decoded to space; split on `&` then the
  first `=`; `;` is not a separator; invalid percent-encoding or non-UTF-8 fails the request (`net/l7/rest.rs:632-690`). Query matcher globs use `.` as separator
  (empty delimiter list defaults to `.`, so `*` does not match a value containing a dot, `schema.mdx:587-590`). **Allow side**: every configured key must be present and
  **all** values of that key must match; **deny side**: every configured key must be present and **at least one** value per key must match (`rego:364-402,794-837`;
  tests `net/opa.rs:6222-6260`).
- **Wildcard hosts + rules**: allowed. Endpoint host matching and rule matching are independent (test `wildcard_host_l7_rules_apply`, `net/opa.rs:10638-10690`).

### 2.2 What the path is matched against (canonicalization) [code + test]

`canonicalize_request_target` (`net/l7/path.rs:115-219`), called on every parsed request (`net/l7/rest.rs:285`) and on the forward-proxy path (comment `net/proxy.rs:5340ff`):

- Rejects (request refused as a protocol violation): NUL/control bytes (raw or via `%00`..`%1F`,`%7F`), raw non-ASCII bytes, fragments (`#`), path > 4096 bytes
  (`MAX_PATH_LEN`, `net/l7/path.rs:99`), malformed percent-encoding, `..` that would climb above `/`, any `.`/`..` surviving canonicalization, and `%2F` unless the endpoint sets
  `allow_encoded_slash: true` (then `%2F` is kept as upper-case `%2F` inside its segment).
- Percent-decodes every `%HH`, then **re-encodes** any byte outside `[A-Za-z0-9-._~!$&'()*+,;=:@]` as **upper-case** `%HH` (`net/l7/path.rs:357-380`). So rule patterns must be written
  in that canonical encoded form (e.g. `%C3%A9`, not `é`) [inference for non-ASCII].
- Resolves `.` and `..` (also when percent-encoded) per RFC 3986 5.2.4; collapses doubled slashes (`//public//../secret` -> `/secret`); a trailing `/` is preserved (`/a/b/..` -> `/a/`).
- Strips `;params` from each segment **before** dot-resolution (`..;/` cannot smuggle), and because stripping happens after decoding, `%3B` is stripped too (`net/l7/path.rs:181-192`; tests `net/l7/path.rs:528-545,641-665`).
- Absolute-form targets lose their authority; origin-form targets containing `://` stay origin-form. The query string is split off at the first `?`, preserved byte-for-byte on the
  wire (`net/l7/rest.rs:602-622`) and parsed separately as above. `%3F` stays `%3F` inside the path.
- The canonical bytes are both what policy sees **and** what is written to the upstream (the request line is rewritten if it changed).
- Before policy evaluation the target is also credential-redacted: placeholder tokens in the path become the literal `[CREDENTIAL]` (`core/secrets.rs:1383-1415`;
  `net/l7/relay.rs:1329`), so a path rule for `/bot<token>/sendMessage` must be written like `/bot*/sendMessage` [inference from code path].
- HTTP header hygiene: bare LF, invalid UTF-8, duplicate Content-Length, missing Host on HTTP/1.1 and Host/absolute-form mismatch are rejected (`net/l7/rest.rs:225-290,484-502`);
  request `Host` must equal the authorized tunnel endpoint or the proxy answers `request_authority_mismatch` (`net/l7/relay.rs:252-272`); HTTP header block limit is 16,384 bytes inside inspected tunnels (`net/l7/rest.rs:45,203`) and 8,192 bytes for the initial proxy request (`net/proxy.rs:91`).
  Inspected body limits default to 65,536 bytes for GraphQL/JSON-RPC/MCP (`proto/sandbox.proto:187-189,217-219`). On overflow the read errors and the relay
  logs a parse rejection and closes without writing an HTTP response (`net/l7/relay.rs:2412-2444`) [inference]; exact client-visible status not determined.

---

## 3. Precedence when several rules match

Not an ordered firewall; **no first-match, no specificity ranking among allows; deny beats allow** (`network-rules.mdx:140-155`).

- **Connection level**: allowed if any rule has a matching endpoint **and** a matching binary (`rego:94-99`). Union across rules. The rule used for logging is the
  lexicographically smallest matching key (`rego:78-87`).
- **Request level**: `_admitted_l7_endpoints` is the union of every endpoint (host/port/`path`-selector match) whose rules allow the request, **provided** `not deny_request`
  (`rego:224-250`). `deny_request` is true when any rule whose endpoint and binary match has a deny rule matching the request (`rego:271-288`). So an allow in one rule and a deny
  in another rule on the same host/port/binary -> denied (test `net/opa.rs:5545-5570`). File order and list order do not matter.
- **Scope of a deny rule**: `deny_request` requires `binary_allowed(policy, input.exec)` for the policy that owns the deny rule (`rego:282-288`). A deny in a rule that lists only binary X does not
  constrain a different binary Y that is allowed through another rule [inference from Rego]. (Binary matching considers the connecting executable **or any ancestor**, `rego:147-172`; the endpoint-only
  standalone proxy ignores `binaries` altogether, `net/opa.rs:359`, `rego:140-142`.)
- **Overlapping endpoints that disagree** are rejected at load, not resolved: endpoints whose host patterns and ports can overlap must agree on `tls`, `allowed_ips`, and explicit-`tcp`-ness;
  inspected endpoints that overlap and have equal path-selector specificity must also agree on `protocol`, `enforcement`, `allow_encoded_slash`, credential rewrite/signing/binding,
  GraphQL/JSON-RPC body limits, `mcp.strict_tool_names` (`pol/ambiguity.rs:60-110,166-205`; gateway call `srv/grpc/policy.rs:2044`). Unequal path specificity (count of non-`*`
  characters in `path`) is allowed and the more specific endpoint's config selects the parser (`net/l7/mod.rs:427-433`; `net/l7/relay.rs:1593-1600`; `pol/ambiguity.rs:423-431`).
- **Endpoint `path` selector vs rule `path`** use different glob dialects: the selector uses `glob::Pattern` semantics where `*` also crosses `/`, empty/`**`/`/**` match all and `/v1/**` also matches `/v1`;
  rule paths use `/`-delimited globset (`core/endpoint_path.rs:10-60`; `schema.mdx:592-595`).
- **GraphQL beats REST on a shared path**: once a GraphQL endpoint's `path` matches, a malformed or disallowed GraphQL operation is denied even if a broader REST rule would allow it (`rego:339-347`).
- **Rules without `protocol` add no request access** to a connection that another matching rule inspects (`network-rules.mdx:143-148`).
- **Provider-contributed rules** are unioned with yours (see 6). A user rule cannot remove them, although a user `deny_rules` entry on a matching endpoint and binary still beats their allows (the overlapping endpoints must agree on `protocol`/`enforcement`, see above); only a gateway-global policy suppresses provider rules.

---

## 4. `protocol: mcp` endpoints

### 4.1 Rule syntax [doc `schema.mdx:334-369`, code `net/l7/mod.rs:863-1000`, `pol/lib.rs:265-300`]

```yaml
rules:
  - allow: { method: tools/call, tool: { any: [search_web, list_issues] } }   # or tool: name, or params: { name: ... }
deny_rules:
  - { method: tools/call, tool: send_email }
```

- `tool` is sugar for `params.name` on `tools/call`; setting both is an error. **No tool-argument matching exists** (`params.<other>` is an error: "MCP tool argument matching is not supported yet").
- Tool matcher = exact name or glob; the glob separator is `.` (empty delimiter list defaults to `.`), so `github.*` matches `github.search` but not `github.search.code`; `**` matches any value
  (`schema.mdx:587-590`). Wildcard tool matchers require `mcp.strict_tool_names: true` (default), which enforces `^[A-Za-z0-9_.-]{1,128}$` on the called tool name (`net/l7/jsonrpc.rs:1124-1136`).
- `method` globs are accepted **only in the `tools/` family** (`tools/*`); `*` and other globs are validation errors (`net/l7/mod.rs:937-951`).
  A rule that allows all of `tools/call` cannot coexist with tool-specific allow rules, and a deny that matches all `tools/call` conflicts with tool allow rules (`net/l7/mod.rs:1498-1512,1651-1666`).
- `mcp.allow_all_known_mcp_methods: true` allows every core method of the request's revision (not extensions); tool-specific allow rules still narrow `tools/call`; deny rules still win
  (`rego:555-568,590-601`).
- `access:` presets are not allowed for mcp/json-rpc (`pol/l7_validate.rs:273-287`).

### 4.2 Methods other than `tools/call`

| Case | Behaviour | Source |
|---|---|---|
| `initialize` (2025 revisions) | Needs its own `allow` rule (or `allow_all_known_mcp_methods`). Must be one non-batched request with an id. Its revision is negotiated in the body, so it is **not** checked against `mcp.versions` (unless the endpoint allows only `2026-07-28`). | `net/l7/mcp.rs:129-160`; `net/l7/jsonrpc.rs:970-1005`; `schema.mdx:447-450,354-369` |
| `notifications/initialized`, `tools/list`, `resources/*`, `prompts/*`, `completion/complete`, `ping`, `logging/setLevel`, `notifications/*`, tasks | "Core" methods: need an explicit `allow` rule per method (or the allow-all option). Only the method is matched; `resources/read` URI and `prompts/get` name are NOT policy-matchable (they are only compared with the `Mcp-Name` header for `2026-07-28`). | `schema.mdx:405-423`; `net/l7/jsonrpc.rs:811-835` |
| Extension method (unknown to every supported revision) | Allowed only by an allow rule naming it **exactly**; globs and `allow_all_known_mcp_methods` never admit it. | `rego:863-869`; `schema.mdx:350-353` |
| Known method that is unavailable in the selected revision or has the wrong direction (e.g. `server/discover` under `2025-11-25`, client-sent `sampling/createMessage`) | Cannot be allowed by any rule. `400 invalid_mcp_request`, even under `audit`. | `net/l7/jsonrpc.rs:921-968`; `schema.mdx:371-378,462` |
| Client-to-server JSON-RPC **responses** (answers to elicitation etc.) | Always admitted on MCP endpoints (generic `json-rpc` hard-denies them). | `rego:570-580`; `net/l7/relay.rs:3000-3012` |
| Legacy GET receive stream (no body) | Admitted once endpoint path and binary match; GET with an operation body is denied. | `rego:603-616`; test `net/l7/relay.rs:11113-11170` |
| `DELETE` (legacy session cleanup) | Rejected `400 invalid_mcp_request` ("Legacy cleanup is unsupported"). | test `net/l7/relay.rs:11113-11170` |
| Server responses / SSE | Relayed without MCP parsing. | `schema.mdx:349`; `network-rules.mdx:539-542` |

- Revision selection: `MCP-Protocol-Version` header, **absent header = `2025-03-26`**, so such clients need `2025-03-26` in `mcp.versions`; default list when omitted is exactly `["2025-11-25"]`
  (`schema-rs:43`; `net/l7/mcp.rs:129-160`). Supported: `2025-03-26`, `2025-06-18`, `2025-11-25`, `2026-07-28` (`schema-rs:31-40`). Duplicate, empty or unsupported header -> 400; supported but not
  allowed -> 403 `mcp_protocol_version_not_allowed` (`net/l7/mcp.rs:58-78`). `2026-07-28` is sessionless, POST-only, needs `params._meta`, mirrors `Mcp-Method`/`Mcp-Name` headers
  (`net/l7/mcp.rs:162-224`; `schema.mdx:465-500`).

### 4.3 JSON-RPC batches

- Only `2025-03-26` accepts batches, at most **64** messages (`MAX_MCP_LEGACY_BATCH_MESSAGES`, `schema-rs:23`; `net/l7/jsonrpc.rs:921-935`); later revisions reject a JSON-array body. `initialize` inside a batch is a lifecycle violation.
- Every member is evaluated separately; **one denied member denies the whole request** (`net/l7/relay.rs:3062-3090`, test `net/l7/relay.rs:8178`). Duplicate JSON object keys anywhere in the body are rejected (`net/l7/jsonrpc.rs:489-600`).
  Generic `json-rpc` has the same batch rule and exact-name-or-`*` methods only (`schema.mdx:502-517`; `net/l7/mod.rs:874-890`).

### 4.4 What gets logged

- One OCSF HTTP Activity event per HTTP request on an MCP/JSON-RPC endpoint (`net/l7/relay.rs:2521-2551`), `firewall_rule(policy_name, "l7-mcp")` (or `"l7-jsonrpc"`, `net/l7/relay.rs:1969`),
  severity Informational for allow/audit, Medium for deny. Message format:
  `JSONRPC_L7_REQUEST decision=<allow|audit|deny> rule_methods=<m1,m2|-> tools=<t1,t2|-> http_method=<..> endpoint=<host:port/redacted-path> policy_version=<n> reason=<..>` (`net/l7/relay.rs:2952-2965`).
- Method names and tool names only; control characters in them become `?`; **parameters/arguments are never logged** (test `net/l7/relay.rs:8464-8520`). For a denied batch the event lists only the denied members.
- MCP rejections carry fixed-text reasons (no caller data): deny-rule hit, extension method without exact allow, tool call without allow rule, core method not permitted (`rego:463-495`).
- Typed HTTP errors: `400` `invalid_mcp_request` / `invalid_mcp_protocol_version_header` / `unsupported_mcp_protocol_version` / `invalid_mcp_request_metadata`; `403` `mcp_protocol_version_not_allowed` and rule denials; `405` `mcp_http_method_not_allowed` (`net/l7/mcp.rs:58-78`).

---

## 5. git smart-HTTP (upload-pack / receive-pack)

- **No special git handling exists in the proxy.** I grepped every source file I fetched (network, policy, core, providers, prover, server policy/validation, sandbox env) for `git-upload-pack`, `git-receive-pack`, `info/refs`, `git-remote`, and `gh search code` lists no other
  runtime hits: matches are only tests, profile YAML, docs and prover comments (`net/opa.rs:6830-6900`, `crates/openshell-providers/src/profiles.rs:4047-4077`, `srv/grpc/policy.rs:11908-11940`). No pkt-line or service-name parsing. [code]
- Git smart HTTP is therefore ordinary **REST** inspection of `GET <repo>.git/info/refs[?service=git-{upload,receive}-pack]`, `POST <repo>.git/git-upload-pack` (clone/fetch) and
  `POST <repo>.git/git-receive-pack` (push). Reference rule set, `providers/github.yaml:55-63` (profile for `github.com:443`, `protocol: rest`, `enforcement: enforce`):
  `GET **`, `HEAD **`, `OPTIONS **`, `POST /**/git-upload-pack` — clone/fetch allowed, push denied. Test: `net/opa.rs:6836-6899`.
  Push is enabled by adding `POST <path>.git/git-receive-pack` (tutorial scopes it to one repo and `GET .../info/refs*`, `docs/tutorials/github-push-access.mdx:145-182`).
  `info/refs` matches on path only; the `service=` query parameter can be matched with a `query` matcher but the references do not.
- Related runtime facts: `GIT_SSL_CAINFO` is injected so git trusts the sandbox CA (`crates/openshell-sandbox/src/child_env.rs:6-21`); Basic-auth placeholders (`user:<placeholder>`) are
  decoded/re-encoded by the proxy (`providers-overview.mdx:394-406`); the profile lists `git`, and `git-remote-https` matches by ancestry (`providers/github.yaml:12`, ancestors in `rego:154-172`).
- **Disagreement inside the repo**: the prover's binary registry labels git smart HTTP as "not REST, bypasses L7 HTTP inspection" (`crates/openshell-prover/registry/binaries/git.yaml:6-10`, `bypasses_l7: true`),
  and the prover raises `l7_bypass_credentialed` for `git`/`ssh`/`nc` reaching a credentialed host (`advisor.mdx:202-207`). The proxy and the profile treat the same traffic as inspectable REST.
  The prover is conservative (it also models git-over-SSH); the proxy behaviour above is what is enforced. Git over `ssh://` or `git://` is plain TCP: only `protocol: tcp` (hostname) is available, with no request rules.

---

## 6. Providers, credential placeholders, `credential_endpoint_mismatch`

- A provider is a gateway object created from an **imported provider profile** (`id`, `credentials`, `endpoints`, `binaries`, refresh/token-grant metadata; schema `profiles.mdx:345-476`,
  struct `crates/openshell-providers/src/profiles.rs:683-720`). The gateway has no built-in profiles (`profiles.mdx:34`, "import-only catalog"). Providers are attached to a sandbox by name
  (`--provider`, `sandbox provider attach`).
- **Policy contribution**: each attached profile-backed provider adds a network rule keyed `_provider_<sanitized-name>` (`pol/compose.rs:8-41,59-75`; built at `srv/grpc/policy.rs:3305-3380`)
  from the profile `endpoints` + `binaries`, unioned with the sandbox's own rules. User rule keys starting `_provider_` are rejected (`srv/grpc/validation.rs:1018-1029`; `schema.mdx:114-115`).
  `policy get --base` is the round-trippable user policy; `--full` is base + provider rules (`manage-policies.mdx:54-82`). While a **global** policy is active, provider rules are not composed
  (`srv/grpc/policy.rs:2024-2030`; `overview.mdx:82-91`).
- **Credential-to-endpoint binding** (the separate second boundary) [doc `providers-overview.mdx:364-392`, `profiles.mdx:49-120`]:
  1. Profile `endpoints` (host, port, `path`) bind **every** static credential the profile declares. Profiles do not map credentials to individual endpoints, so every endpoint of a profile that declares
     credentials is treated as credentialed (`profiles.mdx:493`; `crates/openshell-providers/src/profiles.rs:1096-1105`).
  2. An **endpointless** profile binds only through a sandbox-policy endpoint's `credential_binding: { provider: <attached provider instance> }` (`schema-rs:333-337`). The provider must be attached and
     endpointless; otherwise the whole policy update fails. `credential_binding` is sandbox-scoped and is **rejected in a global policy** (`srv/grpc/policy.rs:3829-3838`).
  3. Network policy and credential binding are independent: allowing a destination does not allow credentials there (`network-rules.mdx:157-166`).
- **Placeholders**: the workload sees opaque placeholders, canonical form `openshell:resolve:env:KEY`, plus revision-scoped `...:v<rev>_KEY`, stable refresh handles `...:s<handle>_KEY`, and a shadow alias form
  `OPENSHELL-RESOLVE-ENV-...` (`core/secrets.rs:13-14,832-844`). Resolution happens in: header values (Bearer, Basic after base64 decode), query parameter values, URL path segments, opted-in textual REST bodies
  (`request_body_credential_rewrite`, UTF-8 JSON/form/text <= 256 KiB, `schema.mdx:175-180`), opted-in client WebSocket text frames (`websocket_credential_rewrite`), and AWS SigV4 signing. Not cookies, responses, other bodies,
  WebSocket binary frames (`providers-overview.mdx:394-409`).
- **`credential_endpoint_mismatch`**: when policy allowed the request but the placeholder's key is outside the binding for this host/port/canonical path, the proxy returns **HTTP 403** with body
  `{"error":"credential_endpoint_mismatch","message":"Credential is not authorized for this request endpoint"}` (`profiles.mdx:156`), emits an OCSF HTTP Activity (denied, High severity, `status_detail
  credential_endpoint_mismatch`, `firewall_rule(policy,"credential-binding")`, `net/proxy.rs:171-196`) and a DetectionFinding `openshell.provider_credential.endpoint_mismatch`
  (`net/l7/mod.rs:38-62`), without recording secret, placeholder, env key or query string. For opted-in WebSocket rewriting the connection is closed with code 1008. Unknown/expired/malformed
  placeholders fail closed too (`providers-overview.mdx:411-420`). The error type is `UnresolvedPlaceholderReason::EndpointMismatch` (`core/secrets.rs:112-120,405-420,476-510`).
  I found no `audit` bypass for this check [inference]: it is a credential-resolution failure, independent of `enforcement`.
- **Credentialed endpoints must be inspected**: an endpoint that is provider-credentialed is blocked at CONNECT if it has no L7 `protocol` or has `tls: skip`, unless `allow_uninspected_credentials: true`
  (`net/l7/mod.rs:463-475`; `profiles.mdx:493`; `schema.mdx:170`). Token-grant profiles additionally reject `protocol: sql` and `tls: skip` (`profiles.mdx:654`).

---

## 7. Private / loopback / link-local / metadata addresses, DNS

### 7.1 Address classes

| Class | Definition | Effect | Source |
|---|---|---|---|
| **Always blocked** | IPv4 `127.0.0.0/8`, `169.254.0.0/16`, `0.0.0.0`; IPv6 `::1`, `::`, `fe80::/10`; IPv4-mapped forms of those (mapped link-local included) | Never connectable under any mode, even with `allowed_ips`. `allowed_ips` entries overlapping them are rejected at policy load. | `core/net.rs:40-79,90-163`; `net/proxy.rs:4621-4660` |
| **Internal** (default-blocked) | Always-blocked plus RFC 1918, CGNAT `100.64.0.0/10`, `192.0.0.0/24`, TEST-NET-1/2/3, `198.18.0.0/15`, `255.255.255.255`; IPv6 ULA `fc00::/7`; mapped IPv4 internals | Blocked for undeclared, wildcard, hostless and advisor-proposed endpoints. Allowed only for (a) exact declared hostnames, (b) IP-literal endpoints (that IP only), (c) addresses listed in `allowed_ips`. | `core/net.rs:165-286`; `net/proxy/destination.rs:93-127`; `schema.mdx:135-149` |
| **Control-plane ports** | 2379, 2380, 6443, 10250, 10255 | Blocked on exact-hostname, IP-literal, `allowed_ips` and host-gateway-alias endpoints (not applied in the default public-only mode, where internal IPs are already rejected). | `net/proxy.rs:4606-4612`; `net/proxy/destination.rs:155-178` |
| **Cloud metadata** | `169.254.169.254` (also `::ffff:169.254.169.254`) is inside the always-blocked link-local range; `CLOUD_METADATA_IPS` is an extra guard on the host-gateway exemption; `metadata.google.internal` is recognized by a static name check (no DNS) in the advisor/prover | Blocked; the advisor refuses to propose it; the prover flags `link_local_reach` for any rule reaching link-local or that name | `net/proxy.rs:229-236,4105-4119`; `core/net.rs:22-30`; `advisor.mdx:203-207,265-270` |
| **Host-gateway aliases** | `host.openshell.internal`, `host.containers.internal`, `host.docker.internal` | Resolved only to the single link-local IP that the compute driver injected into the supervisor's own `/etc/hosts` (read once at startup, before user code runs), or to a backend-pinned IP; a non-link-local or metadata IP disables the exemption. A rule for `host.openshell.internal` can reach services on the gateway host. | `net/proxy.rs:213-217,4098-4170,4192-4240`; `schema.mdx:148-149` |

Observations from reading `is_internal_ip` only [inference]: IPv4 multicast/reserved (`224.0.0.0/3`), IPv6 multicast `ff00::/8`, site-local `fec0::/10`, NAT64 `64:ff9b::/96`, 6to4/Teredo and IPv4-compatible forms are not classified
as internal or always-blocked. The network fence (no direct egress) is the other defence.

### 7.2 Which mode applies

`build_validation_plan` order (`net/proxy/destination.rs:93-127`): host-gateway alias (backend-pinned or trusted) -> endpoint has `allowed_ips` (every resolved address must lie in the list, minus always-blocked) ->
host is an IP literal (that IP only) -> host is an **exact, non-wildcard, user-declared** endpoint (`exact_declared_endpoint_host`, `rego:1040-1054`; advisor-proposed endpoints do not count, `advisor_proposed` flag) ->
default: reject any internal address. Wildcard hosts therefore need `allowed_ips` for private targets (`network-rules.mdx:350-404`; `best-practices.mdx:129-140`).
With `allowed_ips` set, **all** resolved addresses (including public ones) must be inside the list (`network-rules.mdx:397-400`).

### 7.3 Who resolves, and rebinding

- The **supervisor** resolves, not the workload (`docs/about/architecture.mdx:30-32,81-92`: the workload has no direct egress; TCP opens and DNS queries are intercepted and handed over).
  Explicit-proxy flow (`CONNECT host:port` or absolute-form HTTP): lookup order is IP literal -> the sandbox's `/etc/hosts` read via `/proc/<pid>/root/etc/hosts` -> system resolver `lookup_host`
  (`net/proxy.rs:4284-4340`; `best-practices.mdx:136`). All returned addresses are validated; the connection is then dialed to **those same validated socket addresses**
  (`net/proxy/destination.rs:277-312`), so there is no second resolution between check and use (DNS-rebinding window closed on the direct path). [code]
- `/etc/hosts` is therefore a trust input; it is read-only under the default filesystem policy (`default-policy.mdx:28-40`) [inference on the implication].
- Policy-DNS flow (clients that resolve names themselves; applies to **every** endpoint with a host and ports, not only `protocol: tcp`, `network-rules.mdx:650-653`; Rego `policy_dns_eligible_endpoint_records`, `rego:993-1011`): the sandbox's DNS query for a policy-eligible name gets a **synthetic placeholder address** with TTL clamped to 1..30 s
  (`net/policy_dns/mod.rs:47-48`); synthetic addresses come from per-epoch windows of `198.18.0.0/15` (IPv4, /23) and `fd23:6f70:656e::/48` (IPv6, /119), at most 1,024 live mappings (`net/policy_dns/runtime.rs:22-26,50-75`); the supervisor resolves with a `SocketTrustedResolver` pointed at the first literal `nameserver` (port 53) of the **supervisor's own** `/etc/resolv.conf` (`net/policy_dns/runtime.rs:308-328`), never reading sandbox resolver state or `/etc/hosts`
  (`net/policy_dns/resolver.rs:1-4,91-120`; 2 s timeout, 8 KiB messages, <= 16 addresses, <= 8 CNAME hops, `net/policy_dns/resolver.rs:21-24`). Answers are filtered through the same destination plan and **pinned**; a later
  TCP open to the synthetic address is mapped back to the pinned set, stale/unmapped destinations are denied (`net/proxy.rs:927-975`). AAAA queries get an empty answer (`network-rules.mdx:655-657`).
  Names not in policy get no mapping (refusal), though a contract-free observation may be staged so the later TCP attempt can be denied and proposed (`net/policy_dns/mod.rs:149-165`).
- Wildcard hosts authorize DNS for every matching name, a DNS-label exfiltration channel (`network-rules.mdx:644-649`).
- Special names handled by the supervisor itself regardless of policy: `policy.local` (sandbox-local advisor API, synthetic `198.18.0.1`, `net/policy_dns/mod.rs:51`) and a GCE metadata
  emulator on `gcp.metadata.openshell.internal:80` / `127.0.0.1:8174` (`core/google_cloud.rs:15-24`, `net/proxy.rs:5340-5400`, `net/google_cloud_metadata.rs:1-15`); it serves emulated token endpoints from provider credentials, not the real metadata service.

---

## 8. Chaining an upstream egress proxy (`upstreamProxy`)

- `upstreamProxy` is a **Helm value** (`deploy/helm/openshell/values.yaml:89-119`: `url`, `noProxy`, `authSecret.{name,key}`, `authAllowInsecure`, `connectByHostname`, `caBundle.{configMapName,key}`), which maps to
  the gateway/driver options `https_proxy`, `no_proxy`, `proxy_auth_secret_name/key` (k8s) or `proxy_auth_file` (Docker/Podman/VM), `proxy_auth_allow_insecure`, `proxy_connect_by_hostname`, `proxy_ca_bundle`
  (`runtimes.mdx:246-254`; `configuration.mdx:797-835,975-1002,1081-1160`; VM `runtimes.mdx:222`). **It is not a policy field**: operator-owned, set per driver at the gateway, passed to the supervisor as argv, and
  cannot be changed by sandbox environment, image, template `driver_config` or policy (`net/upstream_proxy.rs:4-14`; `runtimes.mdx:132-136`). Standard `HTTPS_PROXY/HTTP_PROXY/ALL_PROXY/NO_PROXY` in the sandbox are ignored (`net/upstream_proxy.rs:21-26`).
- **Traffic covered**: only approved **TLS (HTTP CONNECT) tunnels** after policy + SSRF validation (`dial_upstream`, `net/proxy.rs:4475-4509`, used at `net/proxy.rs:2964`), and policy-DNS **transparent TCP** connections (`dial_transparent_upstream`,
  `4518-4540`). **Plain-HTTP (absolute-form) forward requests always dial directly** (`net/upstream_proxy.rs:15-18`; forward path uses `connector.connect()`, `net/proxy.rs:6568`). Loopback always bypasses.
  Only the final TCP dial changes; policy, DNS and SSRF checks run first. `http://` and `https://` proxy URLs, no SOCKS (rejected), no credentials in the URL; `NO_PROXY` supports hostnames/suffixes/IPs/CIDRs with optional `:port`.
- The CONNECT sent to the corporate proxy targets a **validated resolved IP** unless `proxy_connect_by_hostname` is set (then the proxy resolves the name and becomes part of the egress boundary) (`net/proxy.rs:4475-4510`; `configuration.mdx:1100-1112`).
  Handshake timeout 30 s, response header <= 8 KiB (`net/upstream_proxy.rs:75-80`). Config is fail-closed (invalid settings abort the sandbox's supervisor rather than falling back to direct).
- **Docs contradiction**: `docs/kubernetes/setup.mdx:345` says the initial release does not support HTTPS-to-proxy or custom proxy CA bundles, but `values.yaml:90-119`, `configuration.mdx:797-835` and `net/upstream_proxy.rs:22-34` support both. The statement about plain HTTP not being proxied is correct.

---

## 9. Policy advisor and prover

### 9.1 Proposals: representation

- Wire/storage form is the protobuf **`PolicyChunk`** (`proto/openshell.proto:3254`): `id`, `status` (`pending|approved|rejected`), `rule_name`, `proposed_rule` (a full `NetworkPolicyRule`), `rationale`, `security_notes`,
  `confidence` (agent-authored 0.75, mechanistic 0), `stage`, `hit_count`, `binary`, `validation_result` (prover summary), `rejection_reason`, `application_error`, `review_token`, and the complete current/candidate effective policies and hashes.
  Submitted through gRPC `SubmitPolicyAnalysis` (`analysis_mode` = `agent_authored` or `mechanistic`; `proto/openshell.proto:3327-3345`).
- Two sources: (1) the in-sandbox agent via `http://policy.local/v1/proposals` (JSON, <= 64 KiB, only `addRule` operations, `net/policy_local.rs:63,1036-1060`);
  (2) OpenShell's own drafts from blocked connections (`mechanistic`), always on, one binary -> one host:port. Agent-created endpoints are stamped `advisor_proposed` and so never earn exact-host private-address trust.
  The agent API cannot express `protocol: tcp`, `tls: skip`, GraphQL/MCP rules, query matchers, credential settings or endpoint `path` selectors; `?` in a rule path is refused (`net/policy_local.rs:1137-1215`;
  `pol/l7_validate.rs:132-150`; `advisor.mdx:257-263`). The advisor is off by default (`agent_policy_proposals_enabled`, `core/settings.rs:85`); when off, routes return `404 feature_disabled`.

### 9.2 Approval

- Default is manual review: `openshell rule get|approve|reject <sandbox> --chunk-id ...`, TUI, or gRPC `ApproveDraftChunk` / `RejectDraftChunk` / `ApproveAllDraftChunks` / `EditDraftChunk` / `UndoDraftChunk`
  (`proto/openshell.proto:644-719`). Approval carries a `review_token` that binds the review to the exact live inputs; if inputs changed approval fails `FAILED_PRECONDITION`
  (`srv/grpc/policy.rs:683-690,1702-1740`). Approving merges the rule into the sandbox's **own** policy as a new revision (`AddNetworkRule`, `proto/openshell.proto:2739-2760`) and never edits provider rules.
- Auto mode: setting `proposal_approval_mode` = exactly `"auto"` (gateway scope overrides sandbox scope; any other string is treated as manual, `srv/grpc/policy.rs:1505-1535`). A chunk auto-approves only when the prover delta
  is empty and the destination is not flagged (private IP literal, wildcard host, hostless `allowed_ips`, port > 49152, DB/cache ports) (`advisor.mdx:153-190`; `srv/grpc/policy.rs:1753-1890`).
  While a global policy is active approvals (including automatic) are blocked (`overview.mdx:82-91`, `srv/grpc/policy.rs:1761-1765`).

### 9.3 Prover (Z3)

- Gateway-side **proposal risk check** (delta of "with rule" vs "without"): four categorical findings, no severity: `link_local_reach`, `l7_bypass_credentialed`, `credential_reach_expansion`, `capability_expansion`
  (`crates/openshell-prover/src/finding.rs:29-33`; README). Any finding forces human review. Model inputs are host-coarse (credential presence only, no scopes) and a binary registry (`registry/binaries/*.yaml`).
- Standalone **boundary check**: `openshell-prover check candidate.yaml --boundary boundary.yaml` over the same authored YAML; results `within_boundary` (exit 0), `exceeds_boundary` (1), `error` (2), `unsupported`/`inconclusive` (3);
  coverage domains `filesystem, network_l4, network_rest, process, landlock` (`prover.mdx:49-243`; `crates/openshell-prover/src/containment.rs:153-203`). It requires REST endpoints in `enforce`, exact same filesystem paths in both policies,
  reports `unsupported` for GraphQL/MCP/JSON-RPC/WebSocket rules, REST query matchers, `?`/`[]` in paths, hostless/IPv6/non-ASCII hosts, mixed exact+wildcard hosts without `allowed_ips`; > 1,024 rules or 4,096 endpoints -> `inconclusive`.
  It rejects `metadata`/`review` annotations as unknown input (`crates/openshell-prover/README.md:18`).

---

## 10. Other constraints on compiling a project policy into OpenShell policy

### 10.1 Schema versioning, strictness, limits

- `version` must be `1` (`schema-rs:705-711`). Unknown fields are rejected at every level (`deny_unknown_fields` plus an explicit path-reporting inspector, `schema-rs:730-902`); explicit `null` for object-typed fields is rejected
  (`schema-rs:598-625,1221-1233`); duplicate keys and YAML merge keys (`<<`) are errors; one document; depth <= 64, events <= 300,000, nodes <= 100,000, mapping keys and sequence elements <= 10,000 each, alias expansions <= 100, alias:anchor ratio 5.0 (`schema-rs:540-588`).
  There is **no top-level `metadata`/`review`** key at this tag (a `metadata: null` policy fails, `schema-rs:1221-1233`). `NetworkBinary.harness`, `tls: terminate|passthrough` were removed in 0.1.0 (`docs/upgrade/0-1-0.mdx:99,103`).
  Separate version spaces: gateway TOML is `version = 2`, MCP revisions are an enum, extension protocol is `1.0`; none affects policy `version`.
- Sizes, three different ceilings: YAML file/stream 4 MiB (`schema-rs:557`); inbound gRPC message 1,048,576 bytes (`srv/multiplex.rs:216,256`), which bounds `policy set`;
  `SandboxSpec.policy` encoded size <= 262,144 bytes at sandbox create/governance validation (`srv/grpc/mod.rs:160`, `srv/grpc/validation.rs:267-277`). **`schema.mdx:11` only mentions 4 MiB.** I found no explicit 256 KiB check on the `UpdateConfig` path.
- Other limits: filesystem paths <= 256 total (`read_only` + `read_write`), each <= 4096 bytes, absolute, no `..`, `read_write` not `/` (`pol/lib.rs:1017-1020,1358-1396`); `run_as_user/group` = `sandbox` or numeric 1..=4294967294
  (`pol/lib.rs:840-847`); `network_middlewares` <= 10 configs, selector patterns <= 32, unique `order` (`core/middleware.rs:303,307`; `schema.mdx:533-564`); MCP batch 64; L7 request path <= 4096; header block 16,384 (inspected tunnel) / 8,192 (initial proxy request);
  inspected bodies default 65,536; `policy.local` body 64 KiB; credential-body rewrite <= 256 KiB.
- I found no explicit limit on the number of `network_policies`, endpoints, rules or binaries; they are bounded only by the ceilings above.

### 10.2 Scope: per-sandbox vs gateway-global

- Active policy source order: **global policy**, then the sandbox's saved policy (`--policy` over `OPENSHELL_SANDBOX_POLICY` at creation; later updates replace the saved policy), then a policy baked into the image at `/etc/openshell/policy.yaml`,
  then the restrictive default (`overview.mdx:53-67`; `manage-policies.mdx:40-52`). An invalid image policy blocks start; it does not fall back.
- **Global policy** (platform admin; `openshell policy set --global`): replaces every sandbox's policy in full (no merge), is the complete effective policy (no provider-rule composition), blocks sandbox policy changes and proposal approvals,
  rejects `credential_binding`, and does not support incremental `merge_operations` (`srv/grpc/policy.rs:2024-2037,3805-3840`; `manage-policies.mdx:298-319`). Running sandboxes pick it up at their next config check (~10 s).
- **Sandbox-scoped**: create-time static fields are fixed; later updates may change only network sections and may add (never remove) filesystem paths; `include_workdir`, Landlock and process cannot change (`srv/grpc/validation.rs:1035-1100`).
  `openshell policy update` edits only `network_policies` incrementally; `policy set` replaces the whole thing and **must keep the baseline filesystem paths the supervisor added at startup** (`default-policy.mdx:58-83`).
- Workspaces: sandbox and profile scoping is by named workspace; the `default` workspace must be selected explicitly (`0-1-0.mdx:109`). Settings (`agent_policy_proposals_enabled`, `proposal_approval_mode`) resolve gateway-over-sandbox.

### 10.3 Hot reload

- `network_policies` and `network_middlewares` are **live**; `filesystem_policy`, `landlock`, `process` are startup-only (`schema.mdx:26-37`; `manage-policies.mdx:183-200`). Supervisor polls the gateway every 10 s by default
  (`OPENSHELL_POLICY_POLL_INTERVAL_SECS`); `policy set/update --wait` blocks until the sandbox reports loaded/failed. On load, connections opened under the previous generation (keep-alive, tunnels, WebSockets, streams) are closed and must reconnect.
- Two-stage validation (gateway, then sandbox). The engine reload itself is atomic and leaves the previous engine untouched when the candidate fails (`net/opa.rs:745-775`); what the supervisor then does is set by
  `policy_validation_failure_mode`: `fail_closed` (default: deactivates the previous network policy, closes pinned relays and denies new egress until a valid generation loads) or `retain_last_valid` (`configuration.mdx:308`).
  The setting is gateway-wide and needs a gateway restart.
- New sandboxes get a 300 s window to repair an invalid effective policy before `ProvisioningTimedOut` (`manage-policies.mdx:369-379`).
- A **policy hash** is deterministic (sorted map keys, but list order is significant: endpoints, rules, binaries) (`core/policy_identity.rs:36-80`); re-submitting an unchanged policy is reported as a success that changed nothing (`manage-policies.mdx:260-262`).

---

## Appendix A. Authored policy field reference at v0.1.3

Top level (`schema-rs:176-202`): `version`, `filesystem_policy{include_workdir, read_only[], read_write[]}`, `landlock{compatibility: best_effort|hard_requirement}`, `process{run_as_user, run_as_group}`, `network_policies{<key>: rule}`, `network_middlewares{<key>: mw}`.

`rule` (`schema-rs:239-248`): `name` (display only), `endpoints[]`, `binaries[{path}]` (exact path or `/`-delimited glob; matches the connecting executable or any ancestor; trust-on-first-use SHA-256 pin; empty list matches nothing).

`endpoint` (`schema-rs:256-331`): `host`, `path`, `port`, `ports[]`, `protocol`, `tls`, `enforcement`, `access`, `rules[{allow}]`, `allowed_ips[]`, `deny_rules[]`, `allow_encoded_slash`, `websocket_credential_rewrite`,
`request_body_credential_rewrite`, `allow_uninspected_credentials`, `persisted_queries` (`deny`|`allow_registered`), `graphql_persisted_queries{}`, `graphql_max_body_bytes`, `credential_signing` (`sigv4`|`sigv4:body`|`sigv4:no_body`),
`signing_service`, `signing_region`, `credential_binding{provider}`, `json_rpc{max_body_bytes}`, `mcp{versions[], max_body_bytes, strict_tool_names, allow_all_known_mcp_methods}`.

`allow` / deny entry (`schema-rs:388-465`): `method`, `path`, `command` (SQL, undocumented), `query{}`, `operation_type`, `operation_name`, `fields[]`, `tool`, `params{}`.
`middleware` (`schema-rs:472-498`): `name`, `middleware`, `order`, `config{}`, `on_error` (`fail_closed` default | `fail_open`), `endpoints{include[], exclude[]}`.

Validation cross-rules worth remembering (`pol/l7_validate.rs:243-330`): unknown protocol; tcp + L7 fields; `rules` + `access`; mcp/json-rpc + `access`; json-rpc without rules; protocol without rules/access (except mcp with allow-all);
rules that would deny all; `deny_rules` without protocol or base allow set. Also `credential_signing` needs `signing_service` and cannot combine with `request_body_credential_rewrite` (`pol/lib.rs:1527-1548`).

## Appendix B. Docs vs code, and open points

| # | Topic | Docs say | Code/test shows |
|---|---|---|---|
| 1 | Policy size | "at most 4 MiB" (`schema.mdx:11`) | 4 MiB is the YAML parser budget only; gateway gRPC cap 1 MiB; `spec.policy` <= 256 KiB at create |
| 2 | Host glob alphabet | hosts accept only `*` and `**` (`schema.mdx:584`) | `?`/`[..]` also work and are not rejected; braces/trailing dots unvalidated |
| 3 | `port` + `ports` | not together | both allowed; `ports` wins silently; the mutual-exclusion check looks unreachable |
| 4 | `tls: skip` + protocol | "do not use" (`schema.mdx:156`) | accepted with a warning only; rules not enforced |
| 5 | Protocol list | rest, websocket, graphql, mcp, json-rpc, tcp | also `sql` (passthrough/audit-only) and `command` rule field |
| 6 | Binary symlinks | list real paths, not symlinks (`network-rules.mdx:64-70`) | code also expands symlinked policy paths via `/proc/<pid>/root` when a pid is known (`net/opa.rs:456-463,751-757,2096-2200`) |
| 7 | Upstream proxy on Kubernetes | no HTTPS proxy / CA bundle (`kubernetes/setup.mdx:345`) | Helm values and gateway docs support both |
| 8 | Git smart HTTP | enforceable REST rules (profile, tutorial) | prover registry says git smart HTTP "bypasses L7 inspection" |
| 9 | REST rule `path` | "Required: Yes" (`schema.mdx:263`) | no validator I found rejects a missing `path`; such a rule would never match [inference] |
| 10 | `method` values | "HTTP method, or `*`" | unknown methods only warn |

Not determined from source: client-visible response when an inspected body exceeds `max_body_bytes`; exact `tower-mcp-types` method registry (taken from docs); behaviour of mid-pattern `**` matching zero segments in rule paths
(`/a/**/b` vs `/a/b`) beyond the tested `/**/git-upload-pack`; whether the 256 KiB `spec.policy` ceiling is also applied on `UpdateConfig`.

## Appendix C. Implications for a project-policy compiler (my synthesis, not from the source)

1. Always write `enforcement: enforce` on inspected endpoints; the default is log-and-allow.
2. Emit exactly one of `port`/`ports`, always non-empty; no defaults exist. Lower-case FQDNs without trailing dot; wildcards only as `*.x.y` / `**.x.y` with >= 3 labels.
3. Never emit `tls: skip` with a request `protocol`, never emit `sql`, `command`, top-level `metadata`, or `port`+`ports`. Use `access` XOR `rules`.
4. Deny is cross-rule but scoped to (rule binaries x endpoint host/port/`path`). To make a deny global, repeat it for every rule/binary that can reach the host, and remember provider rules are added on top: user policy can only narrow them through matching deny rules, never remove them (only a global policy suppresses them).
5. Write path patterns in canonical form (decoded, then upper-case re-encoded; no `;params`, `//`, `..`; trailing slash significant; `/x/**` excludes `/x`); put query constraints in `query`, never in `path`. HTTP/1.1 only: gRPC/HTTP2-only upstreams cannot be inspected.
6. MCP: set `mcp.versions` to the revisions clients really send (include `2025-03-26` if the header is absent); allow `initialize` and `notifications/initialized` explicitly; tool control is by name only; extension methods exact; arguments, resource URIs and prompt names cannot be constrained.
7. Credentialed endpoints need an inspected `protocol`; credentials follow profile endpoints (or `credential_binding` for endpointless profiles), not the policy rule that admitted the traffic.
8. Idempotent output: keep list order stable (hash is list-order-sensitive). `policy set` replaces everything: carry over startup-enriched filesystem paths, or restrict updates to `network_policies` with `policy update`.
9. Plan for three size ceilings (4 MiB / 1 MiB / 256 KiB) and the 10 s reload latency; a failed generation fails closed by default.
10. Use the standalone `openshell-prover` boundary check (REST+L4 only, `enforce` required) as the verification gate; anything it labels `unsupported` is outside what it can prove.
