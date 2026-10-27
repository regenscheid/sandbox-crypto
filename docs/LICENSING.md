# Licensing policy

User requirement, confirmed (D45): prefer permissive licenses, ideally publish the project under **MIT**. Use GPL only where it does not affect the project's license. Avoid AGPL. The tiers below come from the plan review ([critical review](../.agents/critical-review.md), finding M1). The project is currently private and personal; this policy keeps an MIT release possible without later substitutions.

License facts below were read from each project's published license file on 2026-10-10. Re-check them when pinning a version.

## Tiers

| Tier | Contents | Allowed licenses | Rule |
| --- | --- | --- | --- |
| 1. Platform code | Web GUI, the reimplemented orchestrator/harness (D11), worker service, adapters, proxies/mirror configuration, monitor, environment manager | MIT, MIT-0, BSD, ISC, Apache-2.0, PSF-2.0, PostgreSQL, public domain, MPL-2.0 (file-level, unmodified) | No GPL/LGPL linking or importing. No AGPL anywhere. Proprietary code cannot be included. |
| 2. Published base worker image (`minimal`) | Minimal runtime: micromamba, Python, jupyter_client/ipykernel, probe and stub scripts | Same as tier 1, plus the system libraries the base distribution requires: Ubuntu's packages, and the GCC runtime, readline, GNU ld and keyutils that conda-forge's Python and ZeroMQ need ([images](IMAGES.md#licensing)) | Keep thin. No GPL research tools baked in. Every build records each package's license and fails on any other license; a standard-format SBOM is still to do. |
| 3. Runtime-installed research dependencies | Sage, fpylll, G6K, PARI/GP, lattice-estimator, FLINT, Poppler, TeX, anything Conda installs on request | Any license the user accepts by installing, **except AGPL by default** | Installed by the user's environment from conda-forge through the mirror into the environment volume. The project does not distribute these binaries. Record each package's license in the environment record. |
| `crypto-base` profile image | Tier 2 plus a pinned set of tier-3 packages (Sage, fpylll, PARI, lattice-estimator, FLINT, TeX, pdf tools, G6K from source) | As tier 3 | The **recipe** (Containerfile and lockfiles) is published under MIT. The **image** is built privately (user's machines or CI), stored in a private registry and never published, because it contains GPL tools. It is the normal base for crypto projects (D46). |

Tier 1 code must work without any tier 3 package installed. It may depend on GPL tools only **over process or protocol boundaries**: subprocess argv/stdio, or the Jupyter messaging protocol to Sage's own kernel. It must not `import sage`, `import fpylll`, or link GPL libraries.

**The project's own packages** are MIT, except for files ported or vendored from Apache-2.0 sources, which keep that license and their notices. A package holding such files declares `MIT AND Apache-2.0`: the orchestrator (`orchestrator/pairing.py`, from OpenAI Codex) and the policy middleware (the OpenShell protobuf contracts and the code generated from them). The Apache License 2.0's text is in [`LICENSES/Apache-2.0.txt`](../LICENSES/Apache-2.0.txt), beside the project's MIT [`LICENSE`](../LICENSE).

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
| OpenShell | Apache-2.0 | Trusted runtime boundary; trusted-worker and trusted-app recipes bundle the exact native v0.1.5 CLI and upstream LICENSE, independently of gateway/driver installation |
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
| pip-audit | Apache-2.0 | Development only: the weekly `audit` workflow checks `uv.lock`'s resolved set against advisories, run with `uv tool run` at a pinned version, never a workspace dependency |
| TeX Live (Ubuntu packages), qpdf | LPPL-1.3c, GPL and others; Apache-2.0 | Tier 3, in the `crypto-base` image |
| miniforge installer | BSD-3-Clause (bundled packages carry their own licenses) | Use conda-forge only; avoid Anaconda `defaults` (commercial terms) |
| jupyter_client, ipykernel | BSD-3-Clause | Kernel protocol: the executor's kernels in the sandbox, and the worker's `kernels` extra for its local backend |
| psutil (launcher on macOS) | BSD-3-Clause | Reads kernel process creation times and session membership for launcher recovery; declared only on Darwin |
| pyzmq, traitlets, jupyter-core, comm, ipython, ipython-pygments-lexers, matplotlib-inline, prompt-toolkit, psutil, nest-asyncio2; tornado, asttokens; debugpy, jedi, parso, executing, pure-eval, stack-data, platformdirs, six, wcwidth; pexpect, ptyprocess; python-dateutil | BSD-3-Clause (nest-asyncio2: BSD); Apache-2.0; MIT; ISC; Apache-2.0 OR BSD-3-Clause | Required by jupyter_client and ipykernel. appnope (BSD, macOS only) and colorama (BSD-3-Clause, Windows only) are locked too |
| KaTeX code/CSS | MIT, with React-derived Apache-2.0 helpers | Math rendering in the workspace GUI (D65, D80) and the isolated math-check bundle (D82). Complete checked primary and embedded notices accompany both distributions, using the same pinned inputs |
| Existing unmodified KaTeX fonts and glyph-derived SVG assets | SIL OFL-1.1, as recorded in the distributed fonts and KaTeX source | Asset-only terms: the GUI ships the exact font bytes and embedded glyph paths with their precise copyright/reserved-name statements and complete pinned official OFL text. The isolated math bundle includes the glyph paths and their exact source attribution/OFL body, without font files. This does not add OFL to the platform-code dependency allowlist |
| Node.js | MIT, with bundled components' notices in the [upstream license](https://github.com/nodejs/node/blob/v24.22.0/LICENSE) | Process runtime for isolated math/figure checks; the unpublished trusted-app recipe bundles native 24.22.0 and its complete upstream LICENSE. It remains outside Python wheels and the restricted orchestrator image |
| PDF.js (`pdfjs-dist` 6.3.289) | Apache-2.0; its character maps (Adobe) and Foxit fonts BSD-3-Clause; its JBIG2 (PDFium) and OpenJPEG decoders BSD | The workspace's PDF viewer ([spec](specs/pdf-viewer.md), PV-5). The GPL-licensed Liberation fonts, the QuickJS engine and the WebAssembly builds are left out; the viewer's license manifest lists each file that ships |
| Vega 6.4.0, Vega-Lite 6.4.3, `vega-interpreter` 2.3.2 | BSD-3-Clause (their d3 dependencies ISC, one bundled package Unlicense) | Tier 1, installed for the figure frame, which draws `vega-lite` charts ([figures](specs/figures.md), FG-15), with expressions run by the interpreter and delimited data read by the frame's own reader, never by generated code. The frame's license manifest lists each bundled package with its notices, `vega-lite`'s own `vega-expression` 6.1.0 among them; the figure check (F7a) uses the same versions, in its own distribution beside the math checker's (`dist-math/figures`), with its notices and its launcher pinned |
| Mermaid 11.17.2 | MIT; its dependencies MIT, ISC, BSD-3-Clause, Apache-2.0 and DOMPurify's MPL-2.0 OR Apache-2.0 | Tier 1, not yet installed: the figure frame and the figure check draw and check `mermaid` diagrams (FG-16). One reviewed exception: **khroma 2.1.0** declares no license in its `package.json` but ships the MIT License as its license file, so the npm check is to accept it by name, version and integrity hash, with its license read from that file; any other version fails until reviewed again. Mermaid 12 is not used while it depends on elkjs |
| elkjs | **EPL-2.0** | Not in tier 1. Mermaid 12 depends on it; it waits until elkjs can be replaced by an empty stub in `stubs/`, as PDF.js's native canvas is |
| vl-convert (`vl-convert-python`) | BSD-3-Clause | Tier 3: installed at runtime in an environment from conda-forge to draw charts for exports (FG-26) |
| **pdf_oxide** (`yfedoseev/pdf_oxide`) | MIT OR Apache-2.0 | **PDF extraction** (D59): the worker's PDF helper runs it in the sandbox (the worker's `pdf` extra; installed in `crypto-base`). Permissive enough for tier 1 or 2. Optional OCR features pull ONNX runtimes (`ort`, `tract-onnx`) and models; check their licenses separately before enabling |
| pypdfium2 | Apache-2.0 | PDF rendering/extraction fallback candidate |
| pdfplumber | MIT | PDF extraction candidate |
| syft, ScanCode | Apache-2.0 | SBOM / license scanning |
| SeaweedFS | Apache-2.0 | Object storage candidate |
| boto3 1.43.115, botocore 1.43.115, s3transfer 0.19.3 | Apache-2.0 | Tier 1, optional orchestrator and worker `s3` extras: authenticated immutable artifact backing and complete-workspace checkpoint transport through the standard SDK credential chain. Installed distribution licenses checked 2026-10-20; no object-store server is embedded |
| jmespath 1.1.0, urllib3 2.8.0 | MIT | Required by the optional S3 SDK. Its python-dateutil and six dependencies are already included in the kernel dependency row above |
| OpenSearch, NATS | Apache-2.0 | Only if search or a broker is ever needed |
| OpenScience web UI (`synthetic-sciences/openscience`, revision `722ad051b7f601bab9e33749aedf942faf8c4737`) | Apache-2.0 | Interaction reference for the session sidebar, optional research pane, keyboard resizing and attachment cards. This change independently implements those behaviors in Svelte; no upstream source, prompts, skills, icons or fonts are copied or distributed. Any future code port must retain per-file license/NOTICE obligations and pass D56 checks. [License](https://github.com/synthetic-sciences/openscience/blob/722ad051b7f601bab9e33749aedf942faf8c4737/LICENSE), [NOTICE](https://github.com/synthetic-sciences/openscience/blob/722ad051b7f601bab9e33749aedf942faf8c4737/NOTICE) |
| OpenScience Desktop upstream (`ai4s-research/open-science`) | MIT | Reuse candidate for code, subject to per-file checks and its NOTICE files: a permissive license does not settle D56. Its two derived skills are never imported (below) |
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
| MCP Python SDK | MIT | A candidate, not used: the MCP proxy (D75) and the harness's MCP client are written on aiohttp |
| OpenCode | MIT | Harness reuse candidate |
| Pydantic AI | MIT | Harness building-block candidate |
| LiteLLM | MIT core (enterprise directory under separate terms) | **Model gateway (D61)**: run the proxy; use only MIT-licensed features |
| OpenAI Codex (`openai/codex`, `rust-v0.163.0` at `6b99197`) | Apache-2.0 | Context management's pairing of tool calls and outputs before a request (CM-29a) is a port of its `context_manager/normalize.rs`, in `orchestrator/pairing.py`, which keeps its license notice and the part of its NOTICE that applies, and says what changed; the orchestrator package declares `MIT AND Apache-2.0`. Nothing else is taken from it |
| sparfenyuk/mcp-proxy, docker/mcp-gateway | MIT | MCP proxy references/possible bases (D67) |
| IBM mcp-context-forge | Apache-2.0 | MCP proxy reference/possible base (D67) |
| markdown-it-py, mdit-py-plugins, mdurl, markdown-it | MIT | Living-document parsing (D65): the orchestrator parses documents with markdown-it-py 4.2.0 and mdit-py-plugins 0.6.1, pinned exactly, and mdurl, which markdown-it-py requires; markdown-it renders the workspace GUI's text (D80). The GUI's math rules are a port of mdit-py-plugins' `dollarmath` (MIT, Copyright (c) 2020 ExecutableBookProject, v0.6.1 at `da70e05`), credited in the ported file, and its rules for living documents' front matter and theorem-like containers are ports of the same package's `front_matter` and `container`, credited in `documentrules.ts`. The server's code span rule is a port of markdown-it 15.0.2's (MIT, Copyright (c) 2014 Vitaly Puzrin, Alex Kocharin), credited in `documents/parser.py`, so that both sides find the same code spans. The rules for LaTeX's own delimiters, on both sides, are adapted from the user's `cscience-crypto`, contributed under the MIT License |
| Svelte | MIT | Workspace GUI framework (D80); its runtime is compiled into the app |
| Vite, @sveltejs/vite-plugin-svelte, rolldown | MIT | Build only: bundle the workspace app |
| lightningcss | MPL-2.0 | Build only: Vite's CSS processing, unmodified |
| TypeScript | Apache-2.0 | Development and CI only: type checks |
| svelte-check, vitest, prettier, prettier-plugin-svelte, happy-dom | MIT (happy-dom's `entities`: BSD-2-Clause) | Development and CI only; happy-dom is the DOM the tests that mount a view run in |
| The workspace app's other npm packages | MIT, except entities (BSD-2-Clause), argparse (PSF-2.0; markdown-it's command line only), aria-query, axobject-query, detect-libc and expect-type (Apache-2.0), picocolors (ISC) and source-map-js (BSD-3-Clause) | Dependencies of the above, pinned by `web/workspace/package-lock.json` and checked one by one in CI |
| pydantic, pydantic-core, annotated-types, typing-inspection | MIT | Policy and API schemas (`packages/policy`) |
| typing-extensions | PSF-2.0 | Required by pydantic; PSF-2.0 is permissive and GPL-compatible |
| PyYAML | MIT | Policy file parsing; living documents' front matter |
| google-re2 (RE2's Python binding, wheels for CPython 3.12 and 3.13 on manylinux x86_64 and aarch64) | BSD-3-Clause | Tier 1: MCP traffic inspection's patterns, which RE2 matches in time linear in the answer ([policy middleware](POLICY-MIDDLEWARE.md#mcp-traffic-inspection)) |
| OpenShell protobuf contracts (`supervisor_middleware.proto`, `extension.proto`, v0.1.5) | Apache-2.0 | Vendored unmodified in `packages/policy-middleware/proto` with the upstream license; the generated Python code carries the same license, and the middleware package declares `MIT AND Apache-2.0` |
| grpcio | Apache-2.0 | Policy middleware gRPC server |
| protobuf | BSD-3-Clause | Policy middleware messages |
| PyJWT | MIT | Policy middleware: verifies OpenShell extension tokens |
| cryptography | Apache-2.0 OR BSD-3-Clause | Ed25519 verification for PyJWT; the launcher's Kubernetes Secret generation (`sandbox-science-kubernetes site`: the site's private CA and server certificates); the throwaway test CA of the test-support package (development only) |
| cffi, pycparser | MIT-0; BSD-3-Clause | Required by cryptography. MIT-0 is MIT without the attribution condition |
| aiohttp | Apache-2.0 AND MIT | HTTP servers and clients: the worker API, the harness's worker client, the workspace server, the package cache, the MCP proxy and the harness's MCP client |
| aiohappyeyeballs, aiosignal, attrs, frozenlist, multidict, propcache, yarl, idna | PSF-2.0; Apache-2.0; MIT; Apache-2.0; Apache-2.0; Apache-2.0; Apache-2.0; BSD-3-Clause | Required by aiohttp |
| pytest, pluggy, iniconfig, pytest-timeout, ruff, pyright | MIT | Development and CI only |
| types-PyYAML, types-grpcio, types-protobuf | Apache-2.0 | Development and CI only (type stubs) |
| grpcio-tools, setuptools | Apache-2.0; MIT | Development only: generates the middleware's protobuf code |
| pytest-aiohttp, pytest-asyncio | Apache-2.0 | Development and CI only (async tests) |
| pytest-xdist, execnet | MIT | Development and CI only (parallel test runs) |
| pytest-repeat | MPL-2.0, unmodified | Development and CI only (repeated test runs that measure flakiness) |
| packaging, pygments, nodeenv | Apache-2.0 OR BSD-2-Clause; BSD-2-Clause; BSD-3-Clause | Development and CI only (pytest and pyright dependencies) |
| Pandoc | GPL | Tier 3: a worker capability installed at runtime (`pandoc`, for document conversion), run as a program of its own |
| Claude Agent SDK (Python) | MIT wrapper that bundles the proprietary Claude Code CLI | **Not** for the MIT core: the runtime it drives is proprietary |
| Claude Agent SDK (TypeScript) | Proprietary (Anthropic Commercial Terms) | Avoid |
| HashiCorp Vault | **BUSL-1.1** | Avoid; use OpenBao or Kubernetes Secrets |
| OpenBao | MPL-2.0 | Secrets-store candidate via OpenShell's vault driver (compatibility to test) |
| External Secrets Operator | Apache-2.0 | Optional Kubernetes secret sync |
| SOPS | MPL-2.0 | Optional encrypted secrets in configuration repositories |
| Anthropic `skills` PDF/office skills | Source-available with service restrictions | Learn from them; do not copy |
| OpenScience's `conducting-scientific-research` and `scientific-problem-selection` skills | Derived from Claude Science text | Never imported, adapted or paraphrased (D56; [plan changes](../.agents/PLAN-CHANGES.md)). Check an upstream's NOTICE files before borrowing anything from OpenScience or Open Science Desktop |
| Built-in skills (`skills/builtin/`) | The project's own, under this repository's MIT License | Written anew for this platform ([skills](SKILLS.md#the-built-in-pack)). No text from Claude Science, Anthropic's skills, or OpenScience's `conducting-scientific-research` and `scientific-problem-selection`, which derive from Claude Science text, is copied or paraphrased (D56) |
| `regenscheid/crypto-analysis-skills` | MIT LICENSE proposed in a pull request (2026-10-10); previously none | Confirm the copyright holder before merging: its commits use a NIST address, and works of US federal employees made as part of their duties are not subject to US copyright, which would call for a different notice |
| Docker Desktop | Proprietary, subscription terms for larger organizations | Acceptable for personal use; Colima/Lima/Podman avoid the question |
| Magma | Proprietary | Optional, licensed pools only |

## Enforcement

- CI runs `scripts/check_licenses.py`, which fails when an installed distribution's license is undeclared or outside the tier 1 allowlist (so GPL/LGPL/AGPL/SSPL/BUSL fail).
- CI also runs `scripts/check_licenses.py --image trusted-app` and `--image trusted-worker`, which compare each trusted image's `licenses.txt` with the licenses of its locked Python wheels ([images](IMAGES.md#licensing)).
- For each web app, CI runs `scripts/check_licenses.py --npm` on its `package-lock.json` before installing anything. Every package must declare a tier 1 license, come from the npm registry with a SHA-512 integrity hash, and need no install script unless it is optional; packages install with `--ignore-scripts`. The one exception is a stub in the app's own `stubs/` directory, which an npm override puts in place of a package that must not be installed (the PDF viewer's, for PDF.js's native Node canvas): the app's `package.json` must override that package and depend on it as `file:stubs/<name>`, the lockfile must record exactly a link to the stub and the stub's name, version and tier 1 license, and with `--installed` the stub's directory, reached through no symbolic link, must hold only a `package.json` that declares no dependencies, scripts, executables or entry points. After the install, `--installed` checks each package's own `package.json` against the lockfile's license and version, since the integrity hashes cover the packages but not the lockfile's license fields. Both checks read SPDX expressions with their precedence.
- The base image builds record every package and its license (`/usr/share/sandbox-science/image.json`). The `minimal`, `orchestrator` and `mcp-proxy` builds fail on any license that is not permissive, outside an explicit allowlist of the base distribution's runtime libraries ([images](IMAGES.md#licensing)). A standard-format SBOM for the published image is still to do.
- Record tier 3 package licenses in each environment record (conda `info/licenses`, PyPI metadata). Show AGPL packages in the GUI when they are installed at runtime.
- The provisioning tool refuses an install whose resolved packages include one that declares the GNU Affero GPL, naming the package and its license, before anything downloads, unless the project's policy names the package under `packages.agpl_exceptions` ([policy](POLICY.md), "Packages"; [worker](WORKER.md#provisioning-operations)). Lockfiles record each Conda package's declared license.
- Before copying any code from a reuse source, record its license and revision in the commit.
