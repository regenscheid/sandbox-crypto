# Licensing policy

Date: 2026-10-10. User requirement: prefer permissive licenses, ideally publish the project under **MIT**. Use GPL only where it does not affect the project's license. Avoid AGPL. Proposed by the plan review ([critical review](../.agents/critical-review.md), finding M1). The project is currently private and personal; this policy keeps an MIT release possible without later substitutions.

License facts below were read from each project's published license file on 2026-10-10. Re-check them when pinning a version.

## Tiers

| Tier | Contents | Allowed licenses | Rule |
| --- | --- | --- | --- |
| 1. Platform code | Web GUI, the reimplemented orchestrator/harness (D11), worker service, adapters, proxies/mirror configuration, monitor, environment manager | MIT, MIT-0, BSD, ISC, Apache-2.0, PSF-2.0, PostgreSQL, public domain, MPL-2.0 (file-level, unmodified) | No GPL/LGPL linking or importing. No AGPL anywhere. Proprietary code cannot be included. |
| 2. Published base worker image (`minimal`) | Minimal runtime: micromamba, Python, jupyter_client/ipykernel, probe and stub scripts | Same as tier 1, plus the system libraries the base distribution requires: Ubuntu's packages, and the GCC runtime, readline, GNU ld and keyutils that conda-forge's Python and ZeroMQ need ([images](IMAGES.md#licensing)) | Keep thin. No GPL research tools baked in. Every build records each package's license and fails on any other license; a standard-format SBOM is still to do. |
| 3. Runtime-installed research dependencies | Sage, fpylll, G6K, PARI/GP, lattice-estimator, FLINT, Poppler, TeX, anything Conda installs on request | Any license the user accepts by installing, **except AGPL by default** | Installed by the user's environment from conda-forge through the mirror into the environment volume. The project does not distribute these binaries. Record each package's license in the environment record. |
| `crypto-base` profile image | Tier 2 plus a pinned set of tier-3 packages (Sage, fpylll, PARI, lattice-estimator, FLINT, TeX, pdf tools, G6K from source) | As tier 3 | The **recipe** (Containerfile and lockfiles) is published under MIT. The **image** is built privately (user's machines or CI), stored in a private registry and never published, because it contains GPL tools. It is the normal base for crypto projects (D46). |

Tier 1 code must work without any tier 3 package installed. It may depend on GPL tools only **over process or protocol boundaries**: subprocess argv/stdio, or the Jupyter messaging protocol to Sage's own kernel. It must not `import sage`, `import fpylll`, or link GPL libraries.

MIT is GPL-compatible, so MIT-licensed helper scripts and skills that import a GPL library can keep their MIT source license. Whoever distributes the *combination* takes on the GPL obligations. That is the user, at runtime, not this project.

## Known components

The GUI distribution includes checked complete local package notices and pinned
primary embedded-component notices ([build inputs](../web/workspace/licenses/README.md)).
This includes Svelte's MIT is-promise adaptation. The isolated math distribution
uses the same checked KaTeX primary/embedded inputs and complete file coverage,
without external fonts/CSS or Svelte. Package metadata alone is not the authority
for embedded subcomponents.

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
| quetz, mamba/micromamba | BSD-3-Clause | Conda mirror and installer candidates; micromamba installs the base images' environments and runtime packages |
| Ubuntu 24.04 (Docker Hub `ubuntu`) | Each package's own (GPL, LGPL and permissive) | Base distribution of both base profile images ([images](IMAGES.md)) |
| conda-lock | MIT | Development only: solves the base images' Conda lockfiles, run with `uvx` |
| TeX Live (Ubuntu packages), qpdf | LPPL-1.3c, GPL and others; Apache-2.0 | Tier 3, in the `crypto-base` image |
| miniforge installer | BSD-3-Clause (bundled packages carry their own licenses) | Use conda-forge only; avoid Anaconda `defaults` (commercial terms) |
| jupyter_client, ipykernel | BSD-3-Clause | Kernel protocol |
| KaTeX code/CSS | MIT, with React-derived Apache-2.0 helpers | Math rendering in the workspace GUI (D65, D80) and the isolated math-check bundle (D82). Complete checked primary and embedded notices accompany both distributions, using the same pinned inputs |
| Existing unmodified KaTeX fonts and glyph-derived SVG assets | SIL OFL-1.1, as recorded in the distributed fonts and KaTeX source | Asset-only terms: the GUI ships the exact font bytes and embedded glyph paths with their precise copyright/reserved-name statements and complete pinned official OFL text. The isolated math bundle includes the glyph paths and their exact source attribution/OFL body, without font files. This does not add OFL to the platform-code dependency allowlist |
| Node.js | MIT, with bundled components' notices in the [upstream license](https://github.com/nodejs/node/blob/v24.20.0/LICENSE) | Operator-provided process runtime for the isolated math checker; not bundled in Python wheels or the orchestrator image yet |
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
| User's own `cscience-crypto` wrapper code | The user's own | Reusable where it embeds no upstream code; `src/patches.mjs` embeds upstream fragments and is excluded. The workspace's math repairs and LaTeX delimiter rules are adapted from its `src/web/cscience-math.js` and `pkg/platform/lib/math-repair.mjs`, with their tests, which the user contributed under the MIT License; each ported file says so. Its patches to the upstream Web UI were not ported |
| Anthropic client SDKs (Python, TypeScript) | MIT | Harness model access |
| MCP Python SDK | MIT | Harness and connector tools |
| OpenCode | MIT | Harness reuse candidate |
| Pydantic AI | MIT | Harness building-block candidate |
| LiteLLM | MIT core (enterprise directory under separate terms) | **Model gateway (D61)**: run the proxy; use only MIT-licensed features |
| sparfenyuk/mcp-proxy, docker/mcp-gateway | MIT | MCP proxy references/possible bases (D67) |
| IBM mcp-context-forge | Apache-2.0 | MCP proxy reference/possible base (D67) |
| markdown-it-py, mdit-py-plugins, mdurl, markdown-it | MIT | Living-document parsing (D65): the orchestrator parses documents with markdown-it-py 4.2.0 and mdit-py-plugins 0.6.1, pinned exactly, and mdurl, which markdown-it-py requires; markdown-it renders the workspace GUI's text (D80). The GUI's math rules are a port of mdit-py-plugins' `dollarmath` (MIT, Copyright (c) 2020 ExecutableBookProject, v0.6.1 at `da70e05`), credited in the ported file, and its rules for living documents' front matter and theorem-like containers are ports of the same package's `front_matter` and `container`, credited in `documentrules.ts`. The server's code span rule is a port of markdown-it 15.0.2's (MIT, Copyright (c) 2014 Vitaly Puzrin, Alex Kocharin), credited in `documents/parser.py`, so that both sides find the same code spans. The rules for LaTeX's own delimiters, on both sides, are adapted from the user's `cscience-crypto`, contributed under the MIT License |
| Svelte | MIT | Workspace GUI framework (D80); its runtime is compiled into the app |
| Vite, @sveltejs/vite-plugin-svelte, rolldown | MIT | Build only: bundle the workspace app |
| lightningcss | MPL-2.0 | Build only: Vite's CSS processing, unmodified |
| TypeScript | Apache-2.0 | Development and CI only: type checks |
| svelte-check, vitest, prettier, prettier-plugin-svelte | MIT | Development and CI only |
| The workspace app's other npm packages | MIT, except entities (BSD-2-Clause), argparse (PSF-2.0; markdown-it's command line only), aria-query, axobject-query, detect-libc and expect-type (Apache-2.0), picocolors (ISC) and source-map-js (BSD-3-Clause) | Dependencies of the above, pinned by `web/workspace/package-lock.json` and checked one by one in CI |
| pydantic, pydantic-core, annotated-types, typing-inspection | MIT | Policy and API schemas (`packages/policy`) |
| typing-extensions | PSF-2.0 | Required by pydantic; PSF-2.0 is permissive and GPL-compatible |
| PyYAML | MIT | Policy file parsing; living documents' front matter |
| OpenShell protobuf contracts (`supervisor_middleware.proto`, `extension.proto`, v0.1.3) | Apache-2.0 | Vendored unmodified in `packages/policy-middleware/proto` with the upstream license; the generated Python code carries the same license |
| grpcio | Apache-2.0 | Policy middleware gRPC server |
| protobuf | BSD-3-Clause | Policy middleware messages |
| PyJWT | MIT | Policy middleware: verifies OpenShell extension tokens |
| cryptography | Apache-2.0 OR BSD-3-Clause | Ed25519 verification for PyJWT |
| cffi, pycparser | MIT-0; BSD-3-Clause | Required by cryptography. MIT-0 is MIT without the attribution condition |
| aiohttp | Apache-2.0 AND MIT | HTTP servers and upstream clients of the package cache and the MCP proxy |
| aiohappyeyeballs, aiosignal, attrs, frozenlist, multidict, propcache, yarl, idna | PSF-2.0; Apache-2.0; MIT; Apache-2.0; Apache-2.0; Apache-2.0; Apache-2.0; BSD-3-Clause | Required by aiohttp |
| pytest, pluggy, iniconfig, pytest-timeout, ruff, pyright | MIT | Development and CI only |
| types-PyYAML, types-grpcio, types-protobuf | Apache-2.0 | Development and CI only (type stubs) |
| grpcio-tools, setuptools | Apache-2.0; MIT | Development only: generates the middleware's protobuf code |
| pytest-aiohttp, pytest-asyncio | Apache-2.0 | Development and CI only (async tests) |
| pytest-xdist, execnet | MIT | Development and CI only (parallel test runs) |
| pytest-repeat | MPL-2.0, unmodified | Development and CI only (repeated test runs that measure flakiness) |
| packaging, pygments, nodeenv | Apache-2.0 OR BSD-2-Clause; BSD-2-Clause; BSD-3-Clause | Development and CI only (pytest and pyright dependencies) |
| Pandoc | GPL | Tier 3: runtime-installed in workers for `.tex`/PDF export, invoked as a separate program |
| Claude Agent SDK (Python) | MIT wrapper that bundles the proprietary Claude Code CLI | **Not** for the MIT core: the runtime it drives is proprietary |
| Claude Agent SDK (TypeScript) | Proprietary (Anthropic Commercial Terms) | Avoid |
| HashiCorp Vault | **BUSL-1.1** | Avoid; use OpenBao or Kubernetes Secrets |
| OpenBao | MPL-2.0 | Secrets-store candidate via OpenShell's vault driver (compatibility to test) |
| External Secrets Operator | Apache-2.0 | Optional Kubernetes secret sync |
| SOPS | MPL-2.0 | Optional encrypted secrets in configuration repositories |
| Anthropic `skills` PDF/office skills | Source-available with service restrictions | Learn from them; do not copy |
| `regenscheid/crypto-analysis-skills` | MIT LICENSE proposed in a pull request (2026-10-10); previously none | Confirm the copyright holder before merging: its commits use a NIST address, and works of US federal employees made as part of their duties are not subject to US copyright, which would call for a different notice |
| Docker Desktop | Proprietary, subscription terms for larger organizations | Acceptable for personal use; Colima/Lima/Podman avoid the question |
| Magma | Proprietary | Optional, licensed pools only |

## Enforcement

- CI runs `scripts/check_licenses.py`, which fails when an installed distribution's license is undeclared or outside the tier 1 allowlist (so GPL/LGPL/AGPL/SSPL/BUSL fail).
- For each web app, CI runs `scripts/check_licenses.py --npm` on its `package-lock.json` before installing anything. Every package must declare a tier 1 license, come from the npm registry with a SHA-512 integrity hash, and need no install script unless it is optional; packages install with `--ignore-scripts`. After the install, `--installed` checks each package's own `package.json` against the lockfile's license and version, since the integrity hashes cover the packages but not the lockfile's license fields. Both checks read SPDX expressions with their precedence.
- The base image builds record every package and its license (`/usr/share/sandbox-science/image.json`). The `minimal`, `orchestrator` and `mcp-proxy` builds fail on any license that is not permissive, outside an explicit allowlist of the base distribution's runtime libraries ([images](IMAGES.md#licensing)). A standard-format SBOM for the published image is still to do.
- Record tier 3 package licenses in each environment record (conda `info/licenses`, PyPI metadata). Show AGPL packages in the GUI when they are installed at runtime.
- The provisioning tool refuses an install whose resolved packages include one that declares the GNU Affero GPL, naming the package and its license, before anything downloads, unless the project's policy names the package under `packages.agpl_exceptions` ([policy](POLICY.md), "Packages"; [worker](WORKER.md#provisioning-operations)). Lockfiles record each Conda package's declared license.
- Before copying any code from a reuse source, record its license and revision in the commit.
