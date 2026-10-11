# OpenShell v0.1.3 lab 9 on babbage, 2026-10-11: the M1 exit run

The roadmap's M1 exit, run end to end on a real gateway and torn down afterwards:
- container A orchestrates;
- container B executes at a recorded isolation tier, with egress only through the package cache and the policy;
- Python and Sage kernel state persists;
- files, PDF page evidence, LaTeX results and useful errors return through the same research tools.

Like the earlier labs, this report records observed behaviour.

## What ran where

| | |
| --- | --- |
| Host | babbage: AMD Ryzen AI Max+ 395, Ubuntu 26.04.1 LTS, kernel 7.0.0-38-generic |
| Runtime | OpenShell 0.1.3, VM driver (KVM); sandboxes with 8 vCPUs, 8 GiB of memory and a 16 GiB overlay disk |
| Container B | `ss-env-m1-8ded2341`, which the worker created (D73) from `sandbox-science/crypto-base:02f85b3` (D77). It ran guest kernel 6.12.76 as uid 1000, with a seccomp filter, no-new-privs and no capabilities, and the CPU reported a hypervisor. **Isolation tier 3, declared and in effect**: separate kernel, the worker's sentinel file hidden, no probe findings |
| Container A | `orch-m1`, from `sandbox-science/minimal:02f85b3`, under `compile_orchestrator()`'s policy, which admits only the worker API. The harness (Python 3.14.8) was installed offline from a wheelhouse |
| Policy | The example policy plus a `papers` rule naming `eprint.iacr.org` and `arxiv.org`, entered with `sandbox-science-worker policy add` |
| Services | Policy middleware, package cache and worker on the host, as in labs 2 to 8 |

The crypto-base environment was ready 76 s after the request. That includes exporting its 6.2 GB image from the local Docker store and preparing the image disk. Its executor offered `python` and `sage`, and the `pdf` feature.

## The run

The harness in A ran a scripted model against environment B. Every tool result reported sandbox `ss-env-m1-8ded2341` and isolation tier 3.

| Step | Result |
| --- | --- |
| Fetch a paper | `curl` in B downloaded Regev's "On Lattices, Learning with Errors, Random Linear Codes, and Cryptography" (arXiv:2401.03703, 715,438 bytes, SHA-256 `fea3b21a…`) through the policy's `papers` rule |
| `pdf_info` | 37 pages, each with its printed label, and the outline with each section's page (Introduction p1, Preliminaries p12, Main Theorem p18, …, Public Key Cryptosystem p31) |
| `pdf_search` | Three hits for "learning with errors": `[p1:2]` (the title), `[p15:23]` (the definition) and `[p37:24]` (a reference) |
| `pdf_pages` | Page 1 as 34 numbered lines. The equations came out garbled ("hs , a ₁ i ≈ ε b 1 (mod 2)" for ⟨s, a₁⟩ ≈ε b₁ (mod 2)), as the spec warns |
| `pdf_render` | Page 1 at 100 dpi (850×1100 px), published as an `image/png` artifact whose producer is the attempt that copied it. The render shows the equations as printed |
| Region evidence | The boxes of lines `p1:24` and `p1:25` gave a region, `[239.96, 206.96, 357.64, 245.43]` in points. Rendered at 200 dpi (328×108 px) and published, the crop shows exactly the two equations: a source anchor with an image of what the text says |
| Sage | Finite field identity over GF(17), a polynomial ring over GF(8209) and an exact matrix inverse. With `%display latex`, the result came back as LaTeX, and `M.inverse() * M == 1` as `\mathrm{True}` |
| Useful errors | `1 / 0` came back as `exception`, with `ZeroDivisionError` and its traceback |
| Package egress | `pip download six` succeeded through the cache, with no options (the worker's package-client configuration); `pypi.org` directly was refused |

A first attempt to fetch the paper from `eprint.iacr.org` got HTTP 403 from Cloudflare's bot challenge (`Cf-Mitigated: challenge`). That is the site refusing automated clients, not the platform: the request passed the policy.

## Findings

1. **The orchestrator's sandbox needs its image's paths.** OpenShell's Landlock baseline does not include `/opt/sandbox-science`, where the minimal image's interpreter lives. For the lab, the compiled orchestrator policy got that path by hand. `compile_orchestrator()` now takes a `filesystem` section in the targets file's `orchestrator` entry, like the research sandbox's.
2. **Extraction garbles mathematics; renders do not.** Inner-product brackets and subscripts were lost in the text, while the page and region renders were faithful. The PDF tools already say so. Region crops from line boxes make the check cheap.
3. **Paper sources need naming.** A VM sandbox resolves only host names its policy names, so a project's policy must name the sites its papers come from. Some sites, such as eprint.iacr.org behind Cloudflare, refuse automated clients outright. Fetching papers through the MCP connectors (crypto-research-mcp's ePrint tools) avoids both problems, once the connector auth service exists.

## Cleanup

- **Removed:**
  - both sandboxes and the provider;
  - the units: worker, gateway, middleware and cache;
  - the lab directory (14 GB, mostly the VM driver's prepared image disks).
- **Checked afterwards:**
  - no lab processes;
  - the lab ports free;
  - no OpenShell paths under `~/.config`, `~/.local/state` or `~/.local/share`;
  - no driver socket directories in `/tmp`.
- **Kept:** the two images in the local Docker store, for later labs.
