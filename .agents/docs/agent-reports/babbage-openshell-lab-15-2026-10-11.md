# OpenShell v0.1.3 lab 15 on babbage, 2026-10-11: the MCP proxy image as its sandbox's command

This lab ran the new `mcp-proxy` image in an OpenShell VM sandbox, with its launcher as the sandbox's command, then tore everything down. It answers the three questions the image's pull request left for a lab. Like the earlier labs, this report records observed behaviour.

## What ran where

| | |
| --- | --- |
| Host | babbage: AMD Ryzen AI Max+ 395, Ubuntu 26.04.1 LTS, kernel 7.0.0-38-generic |
| Runtime | OpenShell 0.1.3, VM driver (KVM); sandboxes with 4 vCPUs, 4 GiB of memory and an 8 GiB overlay disk |
| Sandbox | `ss-lab15-proxy`, from `sandbox-science/mcp-proxy:d27e41a`, created with `-- /opt/sandbox-science/bin/mcp-proxy-run`. Its policy came from `compile_mcp_proxy()` with the targets' `binaries: [/opt/sandbox-science/bin/python*]` and `filesystem: {read_only: [/opt/sandbox-science]}`, unedited |
| Servers | DeepWiki's public server, without credentials, and `lab-echo`, a bearer-token server at a name that resolves nowhere, there only to bind provider `lab-echo` (a synthetic token, from an endpointless profile) |
| Settings | `proxy.toml`, the pool ceiling and the policy, uploaded to `/sandbox/mcp-proxy` after the sandbox was ready; the policy bound with `bind` through `openshell sandbox exec` |
| Caller | A script on the host, through `openshell forward service --target-port 8080 --local 127.0.0.1:18080` |

## Results

| Check | Result |
| --- | --- |
| Before the settings | The launcher ran as the sandbox's command and waited |
| After the upload | It started `sandbox-science-mcp-proxy serve --settings /sandbox/mcp-proxy/proxy.toml` within seconds |
| DeepWiki through the proxy | `initialize`, `notifications/initialized` and `read_wiki_structure` worked. The `python*` pattern matched the interpreter, and the compiled filesystem section let it start |
| The provider's placeholder in the sandbox's command | An `exec`'d process cannot read the launcher's or the proxy's environment (`/proc/<pid>/environ`: permission denied). But a call to `lab-echo` failed with `upstream_unavailable`, the unreachable host, and not `credential_unavailable`, which the proxy reports when its environment lacks the placeholder. So the placeholder reached the command |
| `sandbox stop`, then `start` | Ready again in 7 s. The launcher and the proxy came back a few seconds after `Ready`, with the policy store, the binding and the event store as they were. OpenShell's own restart policy for the main process is `never`, so restarts inside the VM are the launcher's job |
| The forward | It ended when the sandbox stopped. Started again, it carried calls to the restarted proxy |
| Killing the proxy | The launcher started it again |

## Findings

1. **The image works as its sandbox's command.** The launcher waits for the settings, runs the proxy with the providers' placeholders, restarts it, and comes back with the sandbox.
2. **The forward does not survive a stop.** Whatever supervises `openshell forward service` must start it again after the sandbox starts, for example a unit that retries.
3. **The proxy comes back a few seconds after `Ready`.** A caller that connects at once may need to retry.
4. **No hand edits.** `compile_mcp_proxy()` with the targets' `filesystem`, which lab 13 had to add by hand, gave a working policy.

## Cleanup

- **Removed:** the sandbox, the provider and its profile, the forward, and the units: gateway, middleware and cache. The lab directory (1.0 GB) was removed.
- **Checked afterwards:** no lab processes, the lab ports free, no OpenShell paths under `~/.config`, `~/.local/state` or `~/.local/share`, no driver socket directories in `/tmp`.
- **Kept:** the images in the local Docker store.
