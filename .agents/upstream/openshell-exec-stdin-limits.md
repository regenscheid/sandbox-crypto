# OpenShell: cumulative exec stdin limit and SSH overflow prevent reliable streaming

**Status.** Ready-to-file draft, not filed externally. The CLI cap and supervisor
overflow response are intentional in this version; this requests a supported,
bounded streaming path rather than removing memory limits.

**Affects.** OpenShell v0.1.3, clean tag commit
`e1f3c82caa3ed3b65de22889ae7ef32a774878ef`. Source locations below are pinned to
that commit. Observed in a disposable OpenShell acceptance lab on 2026-10-17.

## Observed behavior

1. Sending a 100 MiB stdin payload through `openshell sandbox exec` failed with
   the CLI's explicit 4 MiB refusal.
2. Bypassing that CLI via its generated SSH configuration, without a PTY and with
   `OPENSHELL_NO_LOGIN_SHELL=1`, also failed for a 100 MiB burst sent to a streaming
   hash process: exit 74, empty stdout and stderr. A small process-start probe worked.
   This is consistent with the supervisor overflow path below; the saved client
   result alone does not establish the supervisor's exact failure cause or the
   number of bytes pending at failure.
3. Our application workaround uses direct SSH plus stop-and-wait acknowledgements
   for 32 KiB request chunks. Attachment admission for a 104,857,600-byte fixture
   then succeeded in 11.43 seconds. This receipt is not an independent sandbox
   readback/hash result, a general throughput benchmark, or 1 GiB qualification.

Private raw evidence stays outside git. `ssh-transfer-probe.json` has SHA-256
`cceb3f00d8378727f7631906e9d500b69ab4a596f5aee922ce25eba2e618891b`;
`upload-framed-admission.json` has SHA-256
`62ae2efb685e1f8407a7fdc1c9151a0a5d0a2090db11029439fd8545afef2e86`.

## Source explanation

- **CLI lifetime cap:** [`run.rs:1852–1863`](https://github.com/NVIDIA/OpenShell/blob/e1f3c82caa3ed3b65de22889ae7ef32a774878ef/crates/openshell-cli/src/run.rs#L1852-L1863)
  sets stdin to 4 MiB and a 200 ms initial collection grace.
  [`collect_piped_stdin:1943–1966`](https://github.com/NVIDIA/OpenShell/blob/e1f3c82caa3ed3b65de22889ae7ef32a774878ef/crates/openshell-cli/src/run.rs#L1943-L1966)
  refuses an oversized initial prefix. In the continuing streamed path,
  [`run.rs:2645–2678`](https://github.com/NVIDIA/OpenShell/blob/e1f3c82caa3ed3b65de22889ae7ef32a774878ef/crates/openshell-cli/src/run.rs#L2645-L2678)
  counts the prefix and every later chunk together. Consumption never resets that
  counter. Many small RPCs therefore eventually fail too; this is not only a
  single large-file limitation. The CLI explicitly warns that partial input may
  already have been processed.
- **Supervisor pending cap:** [`ssh/input.rs:14–53`](https://github.com/NVIDIA/OpenShell/blob/e1f3c82caa3ed3b65de22889ae7ef32a774878ef/crates/openshell-supervisor-process/src/ssh/input.rs#L14-L53)
  bounds queued plus in-flight bytes to 4 MiB. Capacity returns after writes finish;
  this is **not** a cumulative cap. Its existing
  [`larger-total-transfer test`](https://github.com/NVIDIA/OpenShell/blob/e1f3c82caa3ed3b65de22889ae7ef32a774878ef/crates/openshell-supervisor-process/src/ssh/exec_input_tests.rs#L402-L458)
  sends 5 MiB by waiting for each batch to be consumed.
- **Overflow terminates instead of applying backpressure:**
  [`ssh.rs:878–897`](https://github.com/NVIDIA/OpenShell/blob/e1f3c82caa3ed3b65de22889ae7ef32a774878ef/crates/openshell-supervisor-process/src/ssh.rs#L878-L897)
  explains that russh has already replenished receive credit, and awaiting space
  in this callback would stall other channel work.
  [`reject_exec_input:1022–1046`](https://github.com/NVIDIA/OpenShell/blob/e1f3c82caa3ed3b65de22889ae7ef32a774878ef/crates/openshell-supervisor-process/src/ssh.rs#L1022-L1046)
  schedules process termination and sends exit 74, EOF and close, deliberately
  omitting stderr to avoid blocking on output credit.

## Minimal reproduction and expected behavior

Proposed small reproduction, derived from the source; this exact shortened command
was not rerun for this report. Use a disposable sandbox containing Python:

```sh
python3 - <<'PY' | openshell sandbox exec -n SANDBOX --no-tty --no-login-shell -- python3 -c 'import sys; chunks = iter(lambda: sys.stdin.buffer.read(65536), b""); print(sum(len(c) for c in chunks))'
import sys, time
time.sleep(0.3)  # enter the continuing streamed-input path
for _ in range(160):  # 5 MiB, paced below the supervisor's pending-byte cap
    sys.stdout.buffer.write(b"x" * 32768)
    sys.stdout.buffer.flush()
    time.sleep(0.01)
PY
```

Expected for a supported streaming mode: 5,242,880 bytes delivered with bounded
resident buffering, or a documented pre-dispatch refusal if streaming is unsupported.
For raw SSH, a burst or temporarily slow reader should exert transport backpressure
while signals, output and sibling channels remain responsive. Test this separately
from the CLI cumulative counter; changing only the CLI cap leaves overflow possible.

## Suggested direction and current workaround

Consider tying SSH receive credit to downstream stdin consumption, or another
explicit bounded flow-control interface, without awaiting space in the shared
session callback. Once that is safe, permit continuing CLI streams without a
lifetime byte counter, retaining appropriate per-request and pending-byte bounds.
Regression coverage should include cumulative small messages, slow consumers,
bursts, EOF drain, disconnect, signals and sibling-channel responsiveness.

`sandbox upload` is an appropriate existing option for staged files; it does not
replace a continuing bidirectional RPC stream. Our local workaround is isolated
SSH with acknowledged application chunks ([transport spec](../../docs/specs/worker-stream-transport.md),
implementation commit `edd3192dd9bc71aa543d37827cdd435778bbe180`). It preserves the
OpenShell boundary and validates the complete request before daemon dispatch.
