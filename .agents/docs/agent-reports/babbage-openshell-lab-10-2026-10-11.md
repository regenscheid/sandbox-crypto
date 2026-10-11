# OpenShell v0.1.3 lab 10 on babbage, 2026-10-11: admission on demand

This lab tried admission on demand (D69) on a real gateway, then tore everything down. When research code connects to a host its policy reaches but does not name, the worker should admit the host. Like the earlier labs, this report records observed behaviour.

## What ran where

| | |
| --- | --- |
| Host | babbage: AMD Ryzen AI Max+ 395, Ubuntu 26.04.1 LTS, kernel 7.0.0-38-generic |
| Runtime | OpenShell 0.1.3, VM driver (KVM); the sandbox had 4 vCPUs, 4 GiB of memory and an 8 GiB overlay disk |
| Sandbox | `ss-env-adm-0c71a28c`, which the worker created from `sandbox-science/minimal:02f85b3` in 15.8 s |
| Policy | The example policy without its provider, plus `deny` rules (http and git, ports 80 and 443) for `www.wikipedia.org` |
| Services | Policy middleware, package cache and worker on the host, as in labs 2 to 9. A slow HTTP server stood in for the MCP proxy on port 8080 |

The sandbox has only a loopback interface, no proxy variables, and `nameserver 127.0.0.53`: every lookup and connection goes through OpenShell's supervisor.

## Run 1: with the host-less endpoint, no drafts

The policy compiled before this lab admitted public addresses with a host-less endpoint and named the hosts the rules mention.
- A lookup of `www.example.com` returned `198.18.0.2`, a placeholder. Connecting failed at once with `[Errno 13] Permission denied`.
- After 30 s, `openshell rule get --status pending` still listed nothing. The sandbox log showed why:

  ```
  NET:REFUSE [MED] DENIED www.example.com [reason:policy_dns_ineligible]
  CONFIG:OBSERVATION [INFO] Policy DNS staged unapproved name www.example.com synthetic=198.18.0.2 for TCP policy review
  Denied staged transparent connection destination=198.18.0.2:443 reason=unapproved DNS observation cannot authorize egress
  NET:OPEN [MED] DENIED /opt/sandbox-science/bin/python3.14(0) -> www.example.com:443 [reason:transparent_tcp_observation_denied]
  ```

In the source, a draft is created only when OpenShell's policy engine refuses the connection. Here the host-less endpoint made the engine allow it. The refusal came from a later check, which writes only this log event ([upstream description](../../upstream/openshell-observation-denials-not-proposed.md)).

## Run 2: without it, drafts and admission

The sandbox was given the same policy without the host-less endpoint (`openshell policy set --wait`, loaded in 3.1 s).
- A connection to `www.iana.org` was refused by the policy engine itself (`endpoint www.iana.org:443 is not allowed by any policy`).
- The supervisor flushed the denial, and a draft appeared 2.8 s later. Its text, as `rule get` prints it, is now the worker's test fixture.

The worker was then restarted with admission on, polling every 2 s. It admitted `www.iana.org` 10.2 s after it started: it compiled the revision again with the host named, applied it with `openshell policy set --wait`, and rejected the draft. The retry got HTTP 200.

| Check | Result |
| --- | --- |
| Time to reach a new host, retrying every second | `www.rfc-editor.org` 9.1 s, `www.w3.org` 12.2 s, `www.ietf.org` 16.2 s, `datatracker.ietf.org` 20.2 s |
| A host a `deny` rule names | `www.wikipedia.org` refused: "rule 'blocked-http' (deny)" |
| A package registry | `pypi.org` refused: "a package registry, reachable only through the package cache" |
| An address | `1.1.1.1` admitted. Its redirect to `one.one.one.one` was admitted in a second round. The last answer was a 403 from Cloudflare's server, after OpenShell, its L7 check and the policy middleware had all allowed the request |
| The middleware still decides | A POST to the admitted `www.iana.org` got 403 with `reason_code: approval_required` |
| OpenShell's record | `openshell rule history` lists each draft as proposed, then rejected. None was approved |

The time to reach a new host is mostly OpenShell's: drafts are flushed every 10 s, and the sandbox polls for policy every 10 s.

## Run 3: an admission interrupts transfers

A Python thread in the kernel streamed a chunked response from the slow server, one chunk per second for 60 s. While it ran, a shell call triggered the admission of `www.rfc-editor.org`.
- When the sandbox loaded the new policy, OpenShell closed the stream after 13 chunks. The server logged `BrokenPipeError` after 14.
- Python's `http.client`, reading line by line, reported the body complete, with no error.
- A raw socket read of a second stream, cut by the admission of `www.w3.org`, ended after a complete chunk with plain end of stream and no terminating chunk. So the client could have noticed; `http.client`'s `peek` hides it.

OpenShell documents that a new policy closes connections opened under the old one ([upstream description](../../upstream/openshell-policy-reload-closes-every-connection.md)).

## Findings and changes

1. **No host-less endpoint.** The compiler no longer emits one. Every host a rule names gets an endpoint, limited to public address ranges when the policy reaches other hosts. Every other host and address waits for admission on demand. As a result:
   - LAN hosts that a rule names exactly now get endpoints, where before they were refused;
   - IP addresses that only `*` rules reach need admission first.
2. **The worker answers drafts, it never approves them.** A draft's rule is raw TCP for one program. The worker names admitted hosts in its own compilation instead, and rejects the draft with its decision as the reason.
3. **The first connection fails.** Research code sees `Permission denied`, and the host works 9 to 20 s later. The harness's instructions now tell the agent to wait and retry.
4. **Admissions interrupt transfers.** The docs say so, and say to check downloads by length or checksum. The worker admits all hosts from one check in one revision.
5. **Text, not JSON.** `openshell rule get` has no JSON output at v0.1.3, so the worker parses its text, checked against the draft captured here.

## Cleanup

- **Removed:**
  - the sandbox;
  - the units: worker, slow server, gateway, middleware and cache;
  - the lab directory (1.1 GB).
- **Checked afterwards:**
  - no lab processes;
  - the lab ports free;
  - no OpenShell paths under `~/.config`, `~/.local/state` or `~/.local/share`;
  - no driver socket directories in `/tmp`.
- **Kept:** the two images in the local Docker store.
