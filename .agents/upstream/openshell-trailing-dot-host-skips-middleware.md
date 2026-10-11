# OpenShell: a host with two trailing dots selects no middleware

**Status.** Kept here, not filed upstream: filing is the user's call. The bug was found by reading the source. The adversarial review of the package policy found it on 2026-10-10 (finding L1), and the citations below were rechecked against the source. It has not been reproduced on a gateway. That needs a container driver, because VM sandboxes cannot reach OpenShell's explicit proxy (babbage lab 1).

**Affects.** OpenShell v0.1.3 (tag commit `e1f3c82caa3e`). It affects workloads that can reach the explicit proxy, which means container drivers. Paths below are relative to the OpenShell repository at that tag.

## Summary

OpenShell normalizes a destination host in different ways at different steps. A host written with two trailing dots, such as `example.com..`, keeps one trailing dot after the proxy's first normalization. Middleware selection then refuses to match any host with an empty label, so even a selector of `**` does not apply. The request runs with no middleware, and a `fail_closed` middleware that should decide every request never sees it.

## Steps to reproduce

1. Use a container-driver sandbox. Give its policy a host-less endpoint (ports 80 and 443, `allowed_ips` of public ranges, `access: full`) and a supervisor middleware selected for every host:

   ```yaml
   network_middlewares:
     policy:
       middleware: my-middleware
       on_error: fail_closed
       endpoints: {include: ["**"]}
   ```

2. Through the explicit proxy, send a request the middleware would deny:

   ```bash
   curl -x "$HTTPS_PROXY" -X POST --data x https://example.com../
   ```

**Expected.** The middleware is called and decides the request. A host OpenShell cannot match should be refused, not passed.

**Actual (from the source).** No middleware is selected, and the endpoint's `access: full` admits the POST.

## Where it happens

1. `normalize_host` strips exactly one trailing dot from a CONNECT or absolute-form target (`crates/openshell-supervisor-network/src/proxy.rs:6837-6839`), leaving `example.com.`.
2. The host-less endpoint admits it, because the name resolves, as a fully qualified name, to public addresses.
3. The authority check trims every trailing dot from both hosts before comparing them, so it passes (`crates/openshell-supervisor-network/src/l7/relay.rs:220-243`).
4. Host-pattern matching returns false for any host with an empty label (`crates/openshell-core/src/host_pattern.rs:141-143`). Middleware selection therefore matches nothing, not even `**`, and the chain is empty.
5. With an empty chain, no middleware is called (`proxy.rs:6264`). For traffic the chain would have to inspect, an empty chain is also "unrestricted" (`crates/openshell-supervisor-network/src/l7/middleware.rs:131-135`).

The gateway-alias check strips a trailing dot again (`proxy.rs:4088-4104`). So `host.openshell.internal..` is probably treated as the backend-pinned gateway host while still escaping middleware selection.

## Impact

- **Every middleware decision is bypassed** for public hosts reached this way: writes, uploads, and package registries such as `pypi.org..`, which a deployment may reach only through a cache.
- **The gateway host may be reachable** through its alias without a middleware.

## Suggested fix

- **Normalize the host once, the same way for every consumer.** Endpoint matching, middleware selection, the authority check and the gateway-alias check should all see one form. Either strip all trailing dots, or reject hosts with empty labels with a 400.
- **Make selection fail closed.** When a `fail_closed` middleware's selector could not be evaluated against a host, deny the request rather than running an empty chain.

## Our mitigation

None is possible in the Sandbox Science policy middleware, because it is never called for these requests. On babbage the VM driver keeps workloads away from the explicit proxy. Until this is fixed, container drivers should use closed-internet policies, meaning no host-less endpoint ([policy](../../docs/POLICY.md), "Residual risks").

Since lab 10 (2026-10-11) the compiler emits no host-less endpoint at all: the worker admits hosts on demand instead (D69). A request to `example.com..` then needs an endpoint that names that host, which the compiler never writes, so the path above is closed in Sandbox Science's policies. The inconsistent normalization remains for other configurations.
