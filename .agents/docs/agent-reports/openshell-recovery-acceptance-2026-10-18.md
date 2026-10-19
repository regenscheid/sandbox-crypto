# OpenShell recovery acceptance — 2026-10-18

Disposable synthetic qualification on babbage. No real credentials, paid model
calls, personal service changes or preserved SNOVA acceptance changes. The peer
drives the production streamed Responses, control, harness and worker paths;
it does not assess model judgment or research quality.

## Revisions and isolation

Initial host and orchestrator inputs were `d20e5fe3`. Orchestrator image
`sandbox-science/orchestrator:d20e5fe3` had ID
`sha256:f4fe00a097849e225663caf3dc7e107025724f177e34d5ac50c6f981bcb95bae`.
OpenShell was 0.1.3. The minimal and crypto-base recipe revision was
`a6d2abbf661b9e7c370f2d4baa656b4cab66214c`; their image IDs were respectively
`sha256:807942fc9243cfdd690f72c06e67d05454b289cf52cda9ac49ffe773989ccc3d`
and `sha256:45ca0db676936fd4be7ec0adfd084cc8193bbb216e39955b306934fa0590f36d`.

The lab owned random root `/var/tmp/ss-recovery-01dce28b`, six uniquely prefixed
transient units and dynamic loopback ports. Gateway credentials, TLS and provider
state were created inside that root. The research VM was
`ss-cf15e0c331862128` / `937e5c9e-9ff3-41c2-9936-0da07e674758`, with one vCPU.
The sustained Sage workload additionally fixed its CPU affinity and BLAS threads
to one. Every interruption checked an owned identity before acting.

## Baseline observations

| Probe | Result and boundary |
| --- | --- |
| Agent VM deleted while a child shell was running | Passed. Explicit root Resume completed the same root and child, four linked attempts, one shell intent. Original stdout was `child-holdingchild-done`; canonical result was marked reconciled. This was a two-work tree. |
| Kernel SIGKILL plus agent VM deletion | Passed. Saved generation 1 was absent after the exact PID/start-time kill. Explicit Resume reconstructed one opted-in literal cell into generation 2; `kernel_restore` said `done`, `cells=1`; exact stdout was `42\n`. No model call reconstructed that literal cell. |
| Worker SIGKILL | Passed. An explicitly restarted worker returned the original live Sage execution, attempt, operation and start timestamp. Operation lookup returned that same job. Launcher supervision was deliberately stopped with `--keep-sandboxes` before the standalone worker probe; automatic worker restart was not qualified. |
| Three-level descendant archive | Passed. Root, delegated parent and leaf all completed. The delegated parent searched its subtree, then read the same leaf archive message ID. Its body contained the exact original shell result; the leaf summary omitted that marker. |
| Saved model-phase stream EOF | Failed on baseline. One actual streamed HTTP response ended before its terminal event. The saved run recorded `error` / `model`, but cleanup rewrote the work reason to `interrupted`; no automatic continuation was admitted. |

The literal replacement initially refused after the default 30-second drain:
`old runtime control connection did not settle; replacement refused`. The shell
started at 21:01:35 UTC, finished at 21:02:15, and control then disconnected.
A single retry after that known condition cleared succeeded; the explicit Resume
began at 21:02:43. This was a bounded refusal while old control work drained,
not evidence of an indefinitely orphaned connection. The replacement fixture now
handles precisely that refusal and retains its timing receipt.

Two fixture errors were kept as failures rather than silently replayed. The first
Sage command used unsupported `sage -python` and exited 2 before the workload ran;
the corrected new operation used the image's Python with `from sage.all`. The
first descendant-tree brief omitted `delegate: true`; nested delegation was
correctly denied. A corrected fresh session explicitly opted in and passed.
Neither setup failure is a harness regression.

## Automatic recovery diagnosis and narrow repair

Baseline root `20261018T210932088282Z-run-2f4f4ee40c25fc09ffe446c6` saved the exact
error `the model gateway's Responses stream ended without a final response`.
`RunService._ended` called `WorkRecords.interrupt_run`; that cleanup updated
already paused/blocked descendants as well as running/waiting work. It therefore
overwrote the durable failure diagnosis before automatic recovery evaluated it.

Host-only patch `f6f028c9`, cherry-picked on the baseline as `21e948a4`, preserved
saved paused/blocked diagnoses while retaining explicit stop behavior. The
orchestrator image remained `d20e5fe3` for this narrow qualification.
Fresh session `automatic-fixed` received one premature EOF at 21:15:04 UTC.
One durable automatic Resume, `resume-09d7607d6db34ef1adf418ae`, admitted the same
work's second attempt at 21:15:12; it finished answered. No explicit Resume or
second owner message was sent. Baseline failure evidence remains separate.

## Final runtime and sustained computation

Final reviewed source/image `84bb0a4e22358aeb42b1a436cab84ae787da462a` was built as
`sandbox-science/orchestrator:84bb0a4e`, image ID
`sha256:6e99eb9089569ff17c3528cad1158bf1b7203d7d78a3f1dfc923324bec1b06bc`.
Both host and runtime used that exact revision for the final narrative probe.
Ten paced synthetic child messages of about 60 KB each crossed a 160,000-token
context ceiling's folding trigger. One model-written fold was accepted on its
first attempt and freed 58,324 estimated context tokens. The request used the
child's `delegated` Responses alias and `high` fold effort; the parent-private
marker was absent. The child retained its original 2,000,000-token grant.
Its fold used 122,794 synthetic tokens; the canonical receipt and its stored copy
counted once in the child's total of 989,567 tokens.

This was one root and one child with one attempt each. It verifies single-attempt
grant accounting and allowance containment, not cumulative grant use across
Resume or exhaustion. No overview was produced: `low` overview effort was pinned,
but its dispatch was not exercised. Synthetic pacing is not a throughput result.

Fresh final-runtime session `automatic-final` independently repeated the EOF
probe. The first attempt saved `error` / `model` at 21:21:34 UTC; one durable
Resume, `resume-306b4f30d2ce43ac1cef7782`, admitted its successor at 21:21:40 and
finished answered without an explicit Resume or second owner message.

The final runtime also accepted an exact 100 MiB authenticated binary upload
(104,857,600 bytes). The streamed client download of its retained original and
the delivered research-VM file both had SHA-256
`4cbf988462cc3ba2e10e3aae9f5268546aa79016359fb45be7dd199c073125c0`.
One synthetic chat message attached it; the workspace delivered the copy before
message admission. The immutable original, message and worker copy were checked
without replaying upload or admission. An initial metadata read used the harness
principal with USER context and was correctly refused with HTTP 403; the corrected
read used the owned workspace principal. This qualifies the real HTTP/worker path,
not browser interaction or artifact publication. Client buffers were bounded;
the server deliberately materializes bounded upload/delivery bodies.

The sustained job is a real Sage finite-field matrix computation: a 32 by 32
matrix over GF(65537), its inverse identity checked every batch, with bounded
30-second output and durable checkpoints. A final digest counts verified batches.
It is a recovery endurance probe, not a cryptanalytic benchmark.
It completed at 22:00:16 UTC with exit 0: 3,600.000010118 seconds of computation
and 249,063,436 verified batches. Execution
`exe-5959d9b419e1693cc93d4517`, attempt `att-12705c9cb06cb028afbb7ef7`,
original operation and PID 594 remained the same through agent VM losses, kernel
loss, worker restart and host/runtime upgrades. There was exactly one start
marker. Final operation lookup returned the original completed execution, and
an independent bounded-buffer recomputation matched the final batch digest:
`5aae1eeb37c078fa3f979869a354e26857757dd58f07302237cfeb907f5ee83a`.

## Boundaries

These probes do not qualify automatic replacement after arbitrary VM/worker loss,
reconstruction of nonliteral or externally bound kernel state, real provider
retry/charging behavior, MCP failure recovery, long-form scientific quality or
prompt-cache hit rates. The baseline agent-loss probes used explicit root Resume.
Automatic continuation was separately demonstrated for the saved model-phase
failure, and the final image's narrative path was exercised with a synthetic peer.
Two baseline graceful launcher stops logged unclosed client-session warnings;
they did not coincide with lost job identity. The lab does not establish their
cause or claim that the later narrative-client cleanup change fixed those warnings.

The fixture passed Ruff formatting/lint, Bash syntax, repository hygiene and the
installed dependency license check. Independent light review cleared the fixture,
including incomplete teardown, literal-result precision and the upload boundary.
Product pytest suites were coordinated separately by the parent; this lab did not
launch an overlapping suite.

## Evidence and cleanup

Forty sanitized JSON receipts are retained outside git at
`/var/tmp/ss-recovery-evidence-01dce28b`. Every receipt was checked against
`SHA256.json`; that manifest's SHA-256 is
`665f712f9174a867abec39bd0bf8918a2617ee3c231d06d610c0b39b545414d2`.

| Receipt | SHA-256 |
| --- | --- |
| `sage-final.json` | `5e8b226c6f98998adc2dc5e88cd58275d2c146af1ce438e8e8cb59351a878d37` |
| `automatic-final-result.json` | `e1e7969ae4c729e9dfad2b17bad707a38b962e462319fec2dba5debf020506f9` |
| `narrative-result.json` | `c553c26ecfccfe2de6a93d5835373c55474e1e07ef0722cb81fb1259aab06f78` |
| `upload-delivered.json` | `744d2e5689c35f91761bbafdbec35565186866950e184e566aa44bca85097346` |
| `cleanup.json` | `abd5789970963caddcac5e432a4c87e77d86aeaa076e636b16f5f95e9bc3b5cd` |
| `root-removed.json` | `56ec92b2492fd4cadbad23de6773db1d9ec05761c8ae64a930dc004857770aa8` |

Complete owned teardown passed at 22:02:07 UTC. The ownership ledger recorded
seven sandbox identities over the run; the only remaining research VM was
deleted and the isolated gateway then listed no sandboxes. All six exact units
were inactive, every owned port was closed, and all 18 observed PID/start pairs
were gone. One driver directory was discovered from those owned processes;
its absence was verified after shutdown. The private lab root, including its
synthetic credentials, databases and upload payload, was removed. The reusable
Docker image remains available. The personal and preserved acceptance instances
were not changed.

Teardown exports only allowlisted JSON and rejects home paths, authorization
strings and key material. Explicit `abort` supports failed probes without
requiring successful-workload preconditions; it retains the ownership checks.
