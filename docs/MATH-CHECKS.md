# Isolated math checks

This is the renderer prerequisite for D82 and LD-53 to LD-55. It does **not**
yet connect saves to findings, deliver closing-round notices, or confirm fixes.
Those require the consistency-review store and durable save/check scheduling.

`orchestrator.mathcheck.MathChecker` runs one formula in a fresh Node process.
It imports the same pinned KaTeX 0.18.9, options, bounded front-matter macros
and deterministic repair rules as the workspace GUI. Formula text is JSON
data; it never selects JavaScript, a shell command, a path or a network endpoint.
The checker makes no model calls and returns no rendered HTML.

The result is `accepted`, `rejected`, or `unavailable`. Only a KaTeX ParseError
proves rejection. Size, macro-expansion and definition skips, missing runtimes,
timeouts and invalid responses are inconclusive. A rejected formula can carry
the first deterministic repair that the same renderer accepts. The source is
unchanged. Model repairs and the shared per-project repair cache remain separate.

## Build and configure

From `web/workspace`, install the locked dependencies with
`npm ci --ignore-scripts`, then run `npm run build:math`. The `dist-math/`
directory contains the standalone `mathcheck.mjs` bundle, `check-math.mjs`
launcher and the KaTeX/project license notices. Distribute the directory
together. It uses the GUI's existing dependency lockfile; no second KaTeX
installation is introduced.

The service operator supplies absolute paths to Node, the launcher and bundle
when constructing `MathChecker`. Node 22.12 or newer is required; acceptance
uses 24.20.0, the existing web CI runtime. Node is not bundled into Python
wheels or the orchestrator image by this slice. No save hook or fallback
silently enables the checker when those paths are absent.

Pass a `Formula` with a stable caller-owned ID, TeX, display mode and effective
macros to `check`, or at most 32 distinct formulas to `check_many`. The caller
must retain the containing revision and location; this component does not
grant evidence access or decide author/session attribution. Results record
the formula-input, bundle and launcher SHA-256 hashes, KaTeX version and Node
runtime version. Each invocation copies bounded trusted code into a private
temporary directory so an operator rollout cannot change the bytes mid-check.

## Bounds and failures

- At most two processes and 32 admitted calls per checker; a batch has at most
  32 unique span IDs. A default two-second deadline includes waiting for a
  process slot; operators can choose 0.05 to 10 seconds.
- Requests are at most 256 KiB, stdout at most 32 KiB and stderr at most 4 KiB.
  No raw stderr is returned to callers. Trusted configuration reads require
  ordinary files and cap the bundle at 8 MiB and launcher at 64 KiB.
- The GUI's 5,000 UTF-16-unit formula limit, 200 macros, 1,000 characters per
  macro, 2,000 expanded macro characters and KaTeX expansion/size limits apply.
  KaTeX is always untrusted, with fresh macro state for each attempt.
- Each Node process has a 96 MiB V8 old-space cap and 4 MiB semi-space cap.
  These are heap controls, **not an operating-system RSS limit**. Deployment
  memory controls remain the operator's responsibility.
- Timeout, cancellation, oversized output and failures kill/reap the process
  group before temporary code is removed. The child inherits no credentials
  or Node startup options from the service environment.
  Cleanup covers descendants remaining in that process group. The fixed
  KaTeX bundle creates no child processes; operator-configured code is trusted.
- IDs, protocol version, engine version, settings, result shape and runtime
  floor are checked. Mismatches produce `unavailable`; a skipped or failed
  check must never close a finding as fixed.

Focused tests exercise actual Node/KaTeX, renderer-tested repairs, effective
macros, process cancellation, output flooding, queue deadlines, invalid UTF-8,
FIFO/missing configuration and protocol mismatches. The web CI job builds the
bundle and runs the Python boundary tests with Node available.
