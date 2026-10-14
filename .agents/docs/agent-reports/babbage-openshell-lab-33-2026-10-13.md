<!-- SPDX-License-Identifier: MIT -->
# OpenShell Lab33: internal child-kernel dispatcher

Root's bounded OpenShell run passed on 2026-10-13. Independent runtime and complete owned-cleanup reviews are clear. The exact public archive (archived outside git, sha256 `6a2af6baa263`) retains 653 UTF-8 records: 3,546,481 original public bytes in a 3,904,374-byte archive, SHA `6a2af6baa2635af89930ce77d25572498385e63fa637ecc7f5db7ede9d05c6b7`. No images or private values/hashes are included. Final delivery review and fresh PR CI remain separate.

## Frozen source and fixture

Runtime source is signed clean `09619990b129d7170f98853f8b31644c0d7f7ffd`; helper SHA `a3fa2086ffdde146d0832d2df70569f6568bf09821650f362010d26304fa93c7`, 85,731 bytes, from signed helper-only `91125f4`. Corrected dispatcher author `28008e1` is exact in source-gate union `7619a4b`. All 753 selected product/test/dependency inputs and 174 earlier accepted evidence files remain unchanged through runtime. Source stayed frozen until complete teardown.

One production CLI/app composition owns one manager and its dispatcher, without a worker HTTP listener. Actual canonical ProjectStore/DelegationRecords transactions prepare two shared sessions and three children: A/B in session1 and C in session2. The original managed environment is `lab33-default-1fdfdfd869ce`, VM `ss-b50815871a8236db`, immutable ID `60b034ee-417d-431e-a865-0f19b080c696`. OpenShell 0.1.3 uses the existing local minimal image; no package installation is part of this lab. The normal policy requires an endpoint-free profile and visibly synthetic provider registration; no external provider or model request is made.

## Actual bounded run

The first whole-helper run exits 0 in 126.379s: 19 submitted cells, three child controls and one session close, within admitted caps of 20/eight/one and one original VM. Eighteen scalar cells succeed; the remaining fixed sleep starts and is cancelled through its original A execution/attempt. A small MAIN/A/B pilot precedes the lifecycle sequence.

- MAIN/A/B retain independent globals 111/222/333 with three simultaneously distinct interpreter PIDs and correct session working directories. A's fixed shared file is actually read by B, MAIN and later C.
- Cancelling A preserves B/MAIN. Explicit A restart establishes a new birth and greater generation, clears A's global, and preserves B/MAIN.
- Fully drained host-owner close/reopen under the original daemon preserves child bindings/kernel pins/current revision and globals. Existing history is required before recovery; it is not reseeded.
- Sticky A close refuses another selected submit without a new execution. Whole session1 close makes MAIN/B passive kernel lists empty while session2 C retains global 444.
- Normal idle suspension retires the actual endpoint to `[]`. Thirty selected-C observations report unavailable using the same initialized owner. Worker/environment/control snapshots remain equal; all traced SQL verb counts, including SELECT, are zero. Safe root gateway observations show the exact original VM Stopped at `21:30:38.738257Z` and `21:30:54.459723Z`, bracketing the reads. The reads take 0.000771s; this finite bracket is not continuous VM-state or scheduling-fairness proof.
- One ordinary session2 MAIN cell resumes the same immutable VM with a new daemon incarnation and lost old global. Old C binding refuses without another execution. Exact original VM deletion completes; old C again refuses.

All selected and ordinary execution markers remain distinct: 11 run-v1 and eight legacy attempts. Original immutable intents, execution/attempt IDs, control receipts and observations retain their scope. Host owners and borrowed stores drain; a separate read-only inspection exits 0 without startup/schema/RPC/new effects.

## Corrections and cleanup

Before runtime, an actual producer-to-collector check caught an extra `phase` field in the original-VM receipt. The minimal signed helper child removes it; original, stopped-ready and reads-done producers pass the actual strict consumer. Original `a9d437f`/`2b653983`, failed/corrected guard fixtures and reviewer bookkeeping failures remain preserved. No whole helper, VM, cell or control is repeated.

The original v3 public export succeeds with 93 records/143,217 bytes. Independent source probes then demonstrate a pathname replacement risk in its still-unactivated teardown. Corrected v4 adopts that existing export through a held directory FD, verifies every original/export byte, and retires LAB exclusively with NOREPLACE rename. It verifies the retired inode against the original held LAB FD before deleting through that FD. Twenty-three independent source/mock cases clear the correction; actual cleanup exits 0 in 0.716s at `21:50:25.582241Z`. No broad sandbox delete or export republication occurs.

Fresh independent cleanup at `21:52:51.430869Z` verifies seven units inactive, six declared ports without listeners, seven original PID/start identities absent, and the exact LAB, retirement parent, two private paths and observed driver path absent. All 93 public records still match. Gateway emptiness is root's actual pre-shutdown observation plus original deletion receipt; the reviewer performs no new post-shutdown gateway RPC or private/SQL/lifecycle action.

## Validation and limits

Fresh full gates at `7619a4b` pass 11,133 Python tests/43 opt-in skips/two existing warnings in 388.54s, using Python 3.13.15, plus required format/Ruff/Pyright/locked/license/protocol checks. Exact-input reuse retains 803 web/24 math and 76 build assets through 152 inputs; no fresh web build or browser check is claimed. Final helper has 51 author source/mock guards, independent closures/schema checks and fresh root scoped formatting, lint, typing, compile/default-description checks. Runtime review independently verifies 38 behavior checks and seven public receipt/identity ties.

The predecessor full run at `2770b96` was interrupted with EXIT2 and is not a full pass. Two old fake kernels gain explicit legacy fields without changing their assertions; corrected full gates pass. Two oversized collection/projection originals remain external with exact metadata only; the bounded collection projection is retained. Tool-only early root fixture failure is qualified rather than reconstructed as a standalone original log.

This is synthetic host admission, not authenticated controller/model/launcher/grant/handback acceptance. It proves Python interpreter state separation, not filesystem or security isolation, scientific correctness, positive Sage operation, replacement-VM adoption, lost-ACK injection, forced crash recovery, monetary accounting or idle scheduling. Direct helper/CLI environments are explicitly sanitized; the user systemd-manager global environment is not audited. Trusted host setup/control/compiler code runs, with no host research execution. The synthetic provider literal is checked only in memory before private removal; later new source/review/capture artifacts are outside that earlier literal window. Raw provider/config/auth values and private-value hashes are never exported.
