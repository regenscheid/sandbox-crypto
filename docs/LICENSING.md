# Licensing policy

Date: 2026-10-10. User requirement: prefer permissive licenses, ideally publish the project under **MIT**. Use GPL only where it does not affect the project's license. Avoid AGPL. Proposed by the plan review ([critical review](../.agents/critical-review.md), finding M1). The project is currently private and personal; this policy keeps an MIT release possible without later substitutions.

License facts below were read from each project's published license file on 2026-10-10. Re-check them when pinning a version.

## Tiers

| Tier | Contents | Allowed licenses | Rule |
| --- | --- | --- | --- |
| 1. Platform code | Web GUI, the reimplemented orchestrator/harness (D11), worker service, adapters, proxies/mirror configuration, monitor, environment manager | MIT, BSD, ISC, Apache-2.0, PostgreSQL, public domain, MPL-2.0 (file-level, unmodified) | No GPL/LGPL linking or importing. No AGPL anywhere. Proprietary code cannot be included. |
| 2. Published base worker image (`minimal`) | Minimal runtime: micromamba, Python, jupyter_client/ipykernel, probe and stub scripts | Same as tier 1, plus the system libraries the base distribution requires | Keep thin. No GPL research tools baked in. Generate an SBOM and license report on every build. |
| 3. Runtime-installed research dependencies | Sage, fpylll, G6K, PARI/GP, lattice-estimator, FLINT, Poppler, TeX, anything Conda installs on request | Any license the user accepts by installing, **except AGPL by default** | Installed by the user's environment from conda-forge through the mirror into the environment volume. The project does not distribute these binaries. Record each package's license in the environment record. |
| `crypto-base` profile image | Tier 2 plus a pinned set of tier-3 packages (Sage, fpylll, PARI, lattice-estimator, FLINT, TeX, pdf tools, G6K from source) | As tier 3 | The **recipe** (Containerfile and lockfiles) is published under MIT. The **image** is built privately (user's machines or CI), stored in a private registry and never published, because it contains GPL tools. It is the normal base for crypto projects (D46). |

Tier 1 code must work without any tier 3 package installed. It may depend on GPL tools only **over process or protocol boundaries**: subprocess argv/stdio, or the Jupyter messaging protocol to Sage's own kernel. It must not `import sage`, `import fpylll`, or link GPL libraries.

MIT is GPL-compatible, so MIT-licensed helper scripts and skills that import a GPL library can keep their MIT source license. Whoever distributes the *combination* takes on the GPL obligations. That is the user, at runtime, not this project.

## Known components

| Component | License | Tier / action |
| --- | --- | --- |
| OpenShell | Apache-2.0 | Tier 1 dependency candidate |
| kubernetes-sigs/agent-sandbox | Apache-2.0 | Tier 1 dependency candidate |
| gVisor, Kata Containers | Apache-2.0 | Runtime candidates |
| Podman, Lima | Apache-2.0 | Local runtime candidates |
| Colima | MIT | Local runtime candidate |
| Tetragon, Falco | Apache-2.0 (in-kernel eBPF programs carry their own GPL-compatible terms, loaded as separate programs) | Monitor telemetry candidates |
| Envoy | Apache-2.0 | Egress proxy candidate |
| mitmproxy | MIT | Inspection candidate |
| devpi | MIT | PyPI mirror candidate |
| quetz, mamba/micromamba | BSD-3-Clause | Conda mirror and installer candidates |
| miniforge installer | BSD-3-Clause (bundled packages carry their own licenses) | Use conda-forge only; avoid Anaconda `defaults` (commercial terms) |
| jupyter_client, ipykernel | BSD-3-Clause | Kernel protocol |
| KaTeX | MIT | Math rendering |
| PDF.js | Apache-2.0 | PDF viewer |
| **pdf_oxide** (`yfedoseev/pdf_oxide`) | MIT OR Apache-2.0 | **Primary PDF extraction candidate** (D59; already used by the user). Permissive enough for tier 1 or 2. Optional OCR features pull ONNX runtimes (`ort`, `tract-onnx`) and models; check their licenses separately before enabling |
| pypdfium2 | Apache-2.0 | PDF rendering/extraction fallback candidate |
| pdfplumber | MIT | PDF extraction candidate |
| syft, ScanCode | Apache-2.0 | SBOM / license scanning |
| SeaweedFS | Apache-2.0 | Object storage candidate |
| OpenSearch, NATS | Apache-2.0 | Only if search or a broker is ever needed |
| OpenScience Desktop upstream (`ai4s-research/open-science`) | MIT | Reuse candidate for code, subject to per-file checks |
| SageMath | GPLv2+ (distributions effectively GPLv3) | Tier 3 |
| fpylll | GPLv2+ | Tier 3 |
| G6K | GPLv2 | Tier 3 |
| fplll, FLINT | LGPL | Tier 3 |
| lattice-estimator | LGPLv3+ | Tier 3 |
| Squid | GPL | Avoid; prefer Envoy |
| **PyMuPDF**, pymupdf4llm | **AGPL-3.0** | **Avoid**, including as the default PDF tool in tier 3; use pdf_oxide |
| **MinIO** | **AGPL-3.0** | **Avoid**; use SeaweedFS, cloud object storage or filesystem storage |
| **Grafana** | **AGPL-3.0** | **Avoid**; build monitor views into the GUI |
| marker | Code Apache-2.0; weights under a modified OpenRAIL-M with revenue limits | Flag before adopting; weights are not permissive |
| Claude Science runtime (CScience base) | Proprietary upstream distribution | **Not used** (D11). Behavior is reimplemented; no code, prompts, skill text, schemas or assets are copied (D56). |
| User's own `cscience-crypto` wrapper code | The user's own | Reusable where it embeds no upstream code; `src/patches.mjs` embeds upstream fragments and is excluded |
| Anthropic client SDKs (Python, TypeScript) | MIT | Harness model access |
| MCP Python SDK | MIT | Harness and connector tools |
| OpenCode | MIT | Harness reuse candidate |
| Pydantic AI | MIT | Harness building-block candidate |
| LiteLLM | MIT core (enterprise directory under separate terms) | **Model gateway (D61)**: run the proxy; use only MIT-licensed features |
| sparfenyuk/mcp-proxy, docker/mcp-gateway | MIT | MCP proxy references/possible bases (D67) |
| IBM mcp-context-forge | Apache-2.0 | MCP proxy reference/possible base (D67) |
| markdown-it-py, mdit-py-plugins, markdown-it | MIT | Living-document parsing (D65) |
| Pandoc | GPL | Tier 3: runtime-installed in workers for `.tex`/PDF export, invoked as a separate program |
| Claude Agent SDK (Python) | MIT wrapper that bundles the proprietary Claude Code CLI | **Not** for the MIT core: the runtime it drives is proprietary |
| Claude Agent SDK (TypeScript) | Proprietary (Anthropic Commercial Terms) | Avoid |
| HashiCorp Vault | **BUSL-1.1** | Avoid; use OpenBao or Kubernetes Secrets |
| OpenBao | MPL-2.0 | Secrets-store candidate via OpenShell's vault driver (compatibility to test) |
| External Secrets Operator | Apache-2.0 | Optional Kubernetes secret sync |
| SOPS | MPL-2.0 | Optional encrypted secrets in configuration repositories |
| Anthropic `skills` PDF/office skills | Source-available with service restrictions | Learn from them; do not copy |
| `regenscheid/crypto-analysis-skills` | No LICENSE file at `main` | Add a license (MIT if desired) |
| Docker Desktop | Proprietary, subscription terms for larger organizations | Acceptable for personal use; Colima/Lima/Podman avoid the question |
| Magma | Proprietary | Optional, licensed pools only |

## Enforcement

- Run a license check in CI for tier 1 dependencies (lockfile scan), with deny rules for GPL/LGPL/AGPL/SSPL/BUSL.
- Generate an SBOM for the shipped base image and fail the build on AGPL/GPL findings outside an explicit allowlist of base-OS packages.
- Record tier 3 package licenses in each environment record (conda `info/licenses`, PyPI metadata). Show AGPL packages in the GUI when they are installed at runtime.
- Before copying any code from a reuse source, record its license and revision in the commit.
