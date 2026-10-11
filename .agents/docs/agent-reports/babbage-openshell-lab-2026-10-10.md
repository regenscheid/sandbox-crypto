# OpenShell v0.1.3 lab on babbage, 2026-10-10

Runtime validation of the policy middleware (PR #7, D70) against a real OpenShell gateway on babbage, followed by a full teardown. Unlike the other reports in this folder, this one records observed behaviour, not source inspection.

## Setup

- **OpenShell.** v0.1.3 release binaries in `~/.local/opt/openshell-0.1.3/bin`, checked against the published SHA-256 checksums, GitHub's recorded digests and the release workflow's build attestations (tag commit `e1f3c82caa3e`).
- **Gateway.** VM compute driver (libkrun on KVM), listener TLS with client certificates, and the gateway JWT signing bundle from `openshell-gateway generate-certs --output-dir`. It listened on `127.0.0.1:17670` only and ran as a transient systemd user unit (AppArmor label `unconfined`, `NoNewPrivs: 0`). Telemetry was off, and `DOCKER_HOST` pointed nowhere, so the VM driver pulled images from their registries instead of rootful Docker. All state lived in one lab directory through `XDG_*` overrides.
- **Middleware.** `sandbox-science-policy-middleware serve` on `127.0.0.1:50051`, with TLS from a separate CA and a certificate for `IP:127.0.0.1`. It verified tokens against the gateway's `jwt/public.pem` and was registered as `grpc_endpoint = "https://127.0.0.1:50051"`. With the VM driver, each sandbox's supervisor is a host process, so loopback reaches it.
- **Images.**
  - `nvcr.io/nvidia/base/ubuntu:24.04`: the bootstrap image, 32 MB of layers.
  - `docker.io/library/buildpack-deps:noble-scm`: has curl and git, 86 MB of layers.
- **Test traffic.** GitHub only: `api.github.com` and a clone of `octocat/Hello-World`.

## Results

| Check | Result |
| --- | --- |
| KVM from a user unit | `/dev/kvm` opened read-write |
| First sandbox | Ready in 9 s. Guest kernel 6.12.76 (host 7.0); uid 1000; all capability sets zero, `NoNewPrivs: 1`, seccomp mode 2 with 5 filters. OpenShell's in-guest qualification passed, since the sandbox started |
| OpenShell L7 policy alone | GET allowed; POST to a read-only endpoint denied with OpenShell's `policy_denied` JSON (403); an unlisted host refused; `git clone` allowed |
| Middleware registration | `openshell gateway info` listed `sandbox-science/policy-middleware 0.1.0 (protocol 1.0)`. The rustls client accepted the IP SAN, the middleware accepted the gateway's token and the expected audience matched |
| `ValidateConfig` | A sandbox with the compiled `research-sandbox.yaml` was created. Before the sandbox was bound, its requests were refused |
| GET through the middleware | `api.github.com/zen`: allowed by `read-only-web`, 200 |
| POST | 403 `middleware_denied`, `reason_code: approval_required`; escalation recorded in `events.jsonl` |
| Plain HTTP on port 80 | OpenShell reported scheme `https` on port 80. The middleware decided it as `http:80`, and `read-only-web` matched (the port-scheme fix) |
| WebSocket upgrade | 403 `request_unclassifiable` (from the `Sec-WebSocket-*` headers), before any upstream contact |
| `git clone` through the middleware | `info/refs` and two `git-upload-pack` POSTs allowed by `git-fetch`; git 2.43's bodies passed the upload-pack check |
| 5 MiB body | 403 `middleware_failed`: over `max_payload_bytes`, so fail closed. A 3 MiB body reached the middleware and was escalated |
| Middleware stopped | 403 `middleware_failed`; requests succeeded again after a restart |
| Latency, middleware alone | Measured over TLS on a persistent channel, 200 calls each: median 0.71 ms for a GET, 0.89 ms for a 64 KiB POST (escalated and recorded), 7.8 ms for a 4 MiB body |
| Latency, end to end | About 34 ms for a request the middleware denies, measured by curl in the sandbox. Mostly TLS to OpenShell's proxy and the vsock hop |

## Findings

1. **Policy DNS decides which names a sandbox can reach.** A VM sandbox has no network interface. Its DNS and TCP go over vsock to the host supervisor, whose policy DNS answers only for host names that an endpoint names, exactly or by wildcard. For any other name it stages a synthetic address, and the connection is denied (`policy_dns_ineligible`, then `transparent_tcp_observation_denied`). OpenShell then drafts a policy advisor proposal for that host.
   - The explicit proxy (`127.0.0.1:3128`) lives on the host, so the VM cannot use it.
   - The compiler's host-less endpoint therefore admitted only IP-literal destinations. The compiler now also names every public host the policy mentions (D69, amended).
   - Hosts reached only through `*` rules or the default need on-demand admission (planned).
2. **Wildcard hosts.** OpenShell requires three labels in a wildcard host and rejects `*` and top-level-domain wildcards such as `*.com`. `**.*.*` passes its validator, but it cannot match two-label names such as `github.com`, and it lets the sandbox resolve every longer name, which opens a DNS exfiltration channel the middleware cannot see.
3. **`target.scheme` is a label, not a fact.** Confirmed: plain HTTP to port 80 arrives as `https`.
4. **The policy must be stored before the sandbox exists.** The gateway validates a compiled sandbox policy before it creates the sandbox, so the project policy has to be in the middleware's store first. Added: `sandbox-science-policy-middleware store`.
5. **Smaller details.**
   - The workload cannot open `/dev/zero`; test scripts should use `yes | head -c N`.
   - The default Ubuntu image has neither curl nor git.
   - The gateway's PKCS#8 v2 signing key, which embeds the public key, is not readable by Python's `cryptography` as-is; `openssl pkey` converts it.

## Cleanup

- **Removed:** the sandboxes, the units, the lab directory and the driver's `/tmp/os-1000-*` socket directory.
- **Checked afterwards:**
  - no OpenShell or middleware processes;
  - ports 17670 and 50051 free;
  - no OpenShell paths under `~/.config`, `~/.local/state` or `~/.local/share`.
- **Kept:** the user journal entries of the transient units, and the installed OpenShell binaries.
