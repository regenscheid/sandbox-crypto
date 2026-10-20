# Kubernetes persistence increments, 2026-10-20

Continuation on `k8s` after the [foundations](k8s-foundations-2026-10-20.md).
No cluster context, private site configuration or real credentials were accessed.
No models were called and no existing research service was restarted.

## Durable uploaded originals

New uploads use the verified content-addressed blob store, including its optional
S3 backing, instead of adding their bytes to control SQLite. Immutable legacy
inline rows remain readable and unchanged. The appended schema and union inventory
preserve ordering, retention accounting and exact upload identity across both
representations. Archive/evict includes the new upload references.

Binary HTTP uploads and downloads stream in bounded chunks. Charged transfer
ownership covers partial writes, process loss, remote installation and ambiguous
metadata commit outcomes. Retrying the same upload identity reconciles its saved
metadata and digest; a different body cannot replace it. No SQL transaction spans
HTTP body or S3 I/O. Cancellation drains an acquired writer or download reader
before releasing its ownership. Admission bounds concurrent scratch and retained
bytes; uncertain remote effects remain charged rather than swept as orphans.

The 100 MiB HTTP roundtrip test stays below 32 MiB of traced Python allocation.
Focused storage, process, HTTP and artifact checks passed 127 tests with one
integration skip; ten additional repeated cancellation cases passed. These cover
zero-length content, immutable retries/conflicts, cross-process ownership,
process death after installation, scratch-close failure and cancelled acquisition.
This increment's HTTP memory test uses local backing; the preceding foundations
report separately records the disposable 128 MiB S3-compatible cold-read test.

Independent review corrected unbounded per-identity lock files to a fixed lock
stripe set, and made lease cleanup unconditional after scratch-close failure.
Final review reported no critical, high or medium findings. Worker replicas and
legacy inline uploads are not deleted by this change.

## Retained research packages

The shared research images keep real package bytes under
`/sandbox/.sandbox-science-prefix`, with `/opt/sandbox-science` as the stable
compatibility symlink. Trusted service images retain their separate layout.
Kubernetes profiles require the retained layout by default and pin the original
requirement with sandbox ownership. Required-mode marker creation follows fixed
layout validation; ordinary local executors do not mark a developer Python prefix.

The worker checks the retained identity and package fingerprint before accepting
a replacement executor as the saved environment. Missing or changed state holds
execution and preserves the known revision and package journals. Exact restoration
can clear the prefix-specific hold through a durable acknowledgement handshake;
it does not erase unrelated unfinished provisioning or restore live kernel memory.
The acknowledged guest package epoch is recorded before the first restored kernel
can be pinned to a revision. Recovery reads remain available while execution is held.

Independent adversarial review found and corrected stale cached guest baselines,
the post-restoration epoch mapping, loss of an acknowledgement across worker
restart, and package admission during a pending acknowledgement. The final review
reported no critical, high or medium findings; its final prefix/schema check passed
48 tests. Journal equality is not treated as proof of a consumed event, including
an A-to-B-to-A package sequence.

Local AMD64 minimal image
`sha256:39436c13d24f06e1adf2fd81bc63d232d3a4f0e1f7286f8351a6c695b25fc95d`
passed eight ordinary container checks and nine Landlock checks with networking
disabled. A synthetic pip wheel installed in the first container imported in its
replacement, retaining the same prefix identity and package fingerprint. Both
disposable containers used automatic removal; the owned volume was removed and
its absence verified. No image was published. Its label/tag reflects a precommit
dirty build (`sandbox-science/minimal:38247c47-dirty`); the retained input manifest
matches the final recipe, report, lockfile and shim source bytes independently.

The image evidence is copied and hash-verified under
`/var/tmp/ss-k8s-persistence-a7849f9e/prefix/`. Its 21-file normalized `SHA256SUMS`
manifest has SHA-256
`0d30406355035e542b6deaf680e37b9220f350c63a72dacdf40f9fdc2ba3e7ad`.
The original manifest is preserved separately (SHA-256
`67bb606899fbe976c6079180d23c8882e90b1287e5ed870a20b96d2c95a4f397`).
It retains the initial image-check assertion failure separately from the corrected
passing checks. It does not claim live OpenShell/Kubernetes replacement.

## Combined validation

The complete four-worker sweep on production code at `e10c173e` finished in
701.07 seconds: **14,678 passed, 51 skipped, two failures**. One failure was the
schema table inventory missing the new upload tables; the other was an existing
process-kill recovery test whose half-second lease also applied to the fresh run
and expired under parallel load. The inventory is corrected, and the recovery test
now advances its existing wall-clock fixture past the dead owner's lease while
keeping a normal fresh lease. All interruption, held-input and no-replay assertions
remain. No production behavior was changed to accommodate either test.

After those fixes and the test-fixture typing correction, **all 79 affected
schema-inventory, run-service, prefix and worker-schema tests passed** in 17.03
seconds. The corrected process-kill case also passed **ten parallel repetitions**
in 8.33 seconds. The full sweep retains its original failures; it is not presented
as a second all-green full run. Its warnings were the two existing malformed-fixture
serializer warnings and an IPython temporary-directory fallback during an idle
kernel test.

Ruff, formatting, full strict-source/standard-test Pyright, licensing, generated
protocol and repository-hygiene checks pass. The tier check assigns all 14,731
tests exactly once. The web command sequence passed this continuation: **2,239
tests**, Svelte/TypeScript, formatting, licensing, production and math builds.
There were no frontend changes in these increments. Native/live integration
skips remain explicit; no model budget was consumed.

Combined evidence stays outside Git at `/var/tmp/ss-k8s-persistence-a7849f9e`.
Its 37-file `SHA256SUMS` has SHA-256
`33e1b6cf3b533a4538b2cddb713adcc2d48b31a69f5652db5e06962868bb2372`.
Workspace roots in two diagnostic logs and the disposable recreation helper are
normalized to `<workspace>`; `redactions.json` preserves their original digests.
The aggregate static log retains an initial hygiene refusal while this new report
was untracked; the separate final hygiene receipt records exit 0 after staging it.

| Evidence | SHA-256 |
| --- | --- |
| Full sweep, including its two original failures (`python.log`) | 2fc55ae7de2612fd638f7f361bd50640b4f2746ce853a2305968ea4f73e2b735 |
| Corrected affected modules (`affected-final.log`) | 762f3c0e4869d01df2d06155e2c72ee2cb9c78f85c797f8438a49b995922f498 |
| Ten process-kill repetitions (`lease-repeat.log`) | 1cd7c3df8e200776f8c81869d0d381c18a003de9d773fbe6e0542b9488bb52d3 |
| Aggregate static checks (`static-final.log`) | e8a44f88f821dd8613681caa399a2a4644620c0003dbe96e2dd7140311230b1f |
| Final hygiene receipt (`hygiene-final.log`) | d4e49dd8b59469b9ec9b4026c9796f2e272e46b3e0dfe7486db13f05630cfbf9 |

## Remaining qualification and storage work

- Rebuild/publish both shared research image variants; qualify native ARM64 and
  crypto-base, then OpenShell workspace seeding and actual pod replacement.
- Size claims for the entire native prefix plus research data. The upstream
  2 GiB default is too small for crypto-base. Interrupted upstream initialization
  currently does not resume a partial copy; preserve a failed PVC and diagnose it.
- Add exact-snapshot durable capture acknowledgement and an offline worker-replica
  sweep before considering online deletion. Capture-helper cleanup is not proof
  that control storage retained content.
- Complete whole-workspace archive/restore before releasing active or paused PVCs.
  Artifact storage and prefix continuity alone are incomplete workspace coverage.
- Package trusted cluster services and authenticated callbacks; add manifests,
  single-writer metadata placement, RBAC, network policy and mixed-architecture
  live recovery/isolation acceptance.

These remain in the [Kubernetes plan](../../../docs/KUBERNETES.md). No PVC,
remote object, unknown transfer or worker capture replica was removed.
