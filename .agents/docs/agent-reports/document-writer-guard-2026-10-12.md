# Document writer guard investigation

Date: 2026-10-12. Repository baseline: `df318c2a1b449bbe960084ea2ece737f8ddf30af`
(main after PR #76). Scope: source and documentation inspection only. This
investigation created no services, VMs, mounts, runtime probes or implementation.

## Result and evidence boundary

The current executor cannot prove that every arbitrary direct writer of a
document has closed. LD-49 therefore remains open. Uncertain writer state must
hold replacement and removal; neither a completed call nor a quiet kernel proves
closure. Direct edits remain required: a helper-only lock would not cover them.

A protected document filesystem is a feasibility prerequisite for the runtime
mirror. FUSE is a candidate, not a selected implementation or a proven OpenShell
capability. Its documented file-handle lifetime offers a useful boundary, but
the integration, namespace safety, recovery and resource limits below need
implementation, independent review and real OpenShell acceptance.

The [existing executor](../../../packages/worker/src/sandbox_science/worker/executor.py)
function `being_written` skips unreadable peer descriptors/maps and compares
textual paths. Its docstring explicitly says that `False` is not proof. The
pre/post-exchange checks in `write_file` and `_remove_checked` do not remove that
visibility limit. Hard-link aliases, renamed paths and writers appearing during
a check also require object identity and an admission barrier.

[Lab20](babbage-openshell-lab-20-2026-10-12.md#failure-found-before-merge)
observed peer `environ`, `maps` and `exe` reads denied, and an escaped child still
alive after daemon replacement. The corrected original-VM stop/start fence
proved old-process termination while preserving disk; it lost old kernel memory.
That is proven lifecycle recovery, not a writer guard that preserves healthy
kernels. Lab17's readable open/mmap writer checks and overlay exchange success
likewise do not establish complete peer visibility. No new runtime claim is
made here. The retained Lab20 evidence reports guest Linux `6.12.76`.

## Notification and lease candidates

**inotify cannot supply an exact writer counter.** Identical unread events can
coalesce; its manual explicitly warns against counting file events. It does not
report mmap/msync/munmap activity. Recursive directory watches have installation
races, operations through outside aliases may be missed, and queue overflow
drops events. Installing before first publication does not repair coalescing or
mapping visibility. Rescanning after loss cannot recover the missing baseline
under the observed peer `/proc` restrictions. These are documented limitations,
not newly reproduced behavior. [Linux inotify manual](https://www.man7.org/linux/man-pages/man7/inotify.7.html).

**fanotify notifications are also insufficient.** Consecutive notifications may
merge; mmap activity is unreported, and a mount mark does not see access through
another mount. Permission events are not merged, but permission monitoring has
additional failure semantics: closing the group allows outstanding permission
requests. Permission classes and broad mount/filesystem marks need privileged
setup; the event descriptor's access mode is chosen by the listener and is not
the original opener's mode. [fanotify manual](https://man7.org/linux/man-pages/man7/fanotify.7.html),
[fanotify initialization](https://www.man7.org/linux/man-pages/man2/fanotify_init.2.html).

**A bounded permission queue is not a fail-closed publication fence on the
inspected Linux 6.12.76 source.** `fsnotify_insert_event` returns `2` when the queue
overflows or shuts down. `fanotify_handle_event` destroys that unqueued event and
returns `0`, including for a permission event. Thus an opener can pass before the
listener consumes the overflow notice. Allocation failure is a different path
that denies permission; it must not be confused with queue overflow. Unlimited
queues would sacrifice the required resource bound. This is a control-flow
finding from upstream source, not a runtime overflow reproduction.
[Queue implementation](https://raw.githubusercontent.com/gregkh/linux/v6.12.76/fs/notify/notification.c),
[permission-event handler](https://raw.githubusercontent.com/gregkh/linux/v6.12.76/fs/notify/fanotify/fanotify.c).

**File leases provide kernel assistance, but not this complete guard.** A read
lease checks conflicting write opens and blocks conflicting opens/truncation;
after the lease-break deadline the kernel may forcibly remove it. Leases do not
serialize the document directory's rename/link/unlink operations. A check and a
later replacement are still separate operations. The lease lifetime cannot be
made safe by assuming the publisher always runs before a deadline.
[Linux lease manual](https://www.man7.org/linux/man-pages/man2/F_SETLEASE.2const.html).

OverlayFS adds a qualification issue: `ovl_mmap` maps the real backing file;
`backing_file_mmap` calls `vma_set_file`, which replaces the VMA's file reference
and releases the former one. The generic lease conflict check uses the leased
inode's open-for-write count. A lease on the visible overlay inode therefore
needs a separate proof covering a writable mapping after the overlay descriptor
closes. This is a source-derived concern, not a verified lease bypass in the VM.
[OverlayFS](https://raw.githubusercontent.com/gregkh/linux/v6.12.76/fs/overlayfs/file.c),
[backing-file mapping](https://raw.githubusercontent.com/gregkh/linux/v6.12.76/fs/backing-file.c),
[VMA reference replacement](https://raw.githubusercontent.com/gregkh/linux/v6.12.76/mm/util.c),
[lease conflict checks](https://raw.githubusercontent.com/gregkh/linux/v6.12.76/fs/locks.c).

## Proposed protected-filesystem boundary

libfuse defines `release` as occurring when all descriptors are closed and all
memory mappings are unmapped, with one release per open unless force-unmounted.
`flush` is not that event: duplication/fork may produce several flushes and flush
need not occur. The no-open optimization can bypass open/release callbacks and
must not be used for ownership accounting.
[libfuse 3.16.2 low-level contract](https://raw.githubusercontent.com/libfuse/libfuse/fuse-3.16.2/include/fuse_lowlevel.h).

The proposed design would mount a dedicated document filesystem before any
publication or research process can access it. A trusted server would reserve
writable open/create handles before granting them, retain reservations through
final release, and serialize publication with namespace and mutation requests.
Its backing files and control channel would be inaccessible to research code.
Research code could still use ordinary file APIs, editors and shared mappings;
it would not need a helper lock. This is the design to prove, not a property
established merely by choosing FUSE.

The server must account for in-flight writes and metadata mutations as well as
handle counts. Cached FUSE supports mmap; direct I/O disables shared mmap by
default. Mapping/writeback drain and cache invalidation must be qualified before
capturing or replacing an inode. Passthrough, DAX and other shortcuts stay off
until their lifetimes have an independent proof.
[Linux FUSE I/O documentation](https://raw.githubusercontent.com/gregkh/linux/v6.12.76/Documentation/filesystems/fuse-io.rst),
[kernel file implementation](https://raw.githubusercontent.com/gregkh/linux/v6.12.76/fs/fuse/file.c).

This needs a protected bootstrap/supervisor integration. The pinned OpenShell
v0.1.3 filter blocks mount/new-mount and BPF operations in workloads; its
supervisor prelude also blocks mount operations after privileged bootstrap. A
normal executor child is not a substitute. The feasibility slice must identify
an upstream-supported setup boundary or propose a reviewed upstream extension,
without giving research code capabilities or relaxing its security policy.
[Pinned OpenShell source, commit e1f3c82](https://raw.githubusercontent.com/NVIDIA/OpenShell/e1f3c82caa3ed3b65de22889ae7ef32a774878ef/crates/openshell-sandbox/src/sandbox/linux/seccomp.rs).

## Required proof and acceptance boundary

| Boundary | Required invariant and hostile cases |
| --- | --- |
| Installation and trust | Establish the mount before first publication and before workloads start. Reject an existing unguarded environment as uncertain; mounting over it does not prove old handles closed. Protect server code, backing storage, mount controls and publication authentication from research mutation, signals, inherited descriptors and aliases. |
| Handle lifetime | Reserve each successful writable open/create before replying; never decrement on ordinary close/flush alone. Exercise multiple opens by one PID, dup, fork, descriptor passing, detached descendants, cancellation during open/reply and final release after process exit. Unknown or inconsistent ownership holds publication. |
| Mappings and pending I/O | Exercise shared writable mmap after descriptor close, mappings inherited by fork, mprotect transitions, private mappings and delayed/failed writeback. Drain admitted writes and metadata operations before capture/replacement. Kernel CPU inactivity is irrelevant to handle lifetime. |
| Namespace and identity | Use inode/generation identity, not a textual pathname or PID. Serialize publication with open/create/truncate/link/rename/unlink and directory changes. Exercise atomic editor saves, exchange/overwrite renames, hard links, unlink with live handles, symlinks, parent swaps, stale lookup caches and inode reuse. No writable backing alias may bypass the boundary. |
| Arbitrary-edit compatibility | Preserve ordinary direct edits and local-only stray files; require no cooperative locks. Test editors and Python file/mmap APIs. Document cross-mount rename/link behavior, including `EXDEV`; do not silently substitute a non-atomic operation. Unsupported special paths remain held/refused. |
| Capture and acknowledgement | Capture exact bounded bytes only under a valid guard. Bind capture to filesystem incarnation, inode/generation and digest. A durable raw-edit acknowledgement is retention evidence, not a writer permit: reacquire the guard and verify unchanged identity/content and no writers before restore. A newer edit must remain retained or held. |
| Capacity | Bound handles, active inode identities, requests, bytes and retained captures. Reserve capacity before admitting writable access; deny/backpressure on exhaustion. Never evict active ownership to make room. Exercise capacity exhaustion and blocked consumers while publication competes with new opens. |
| Restart and controller fencing | Worker reconnect to the same live filesystem may preserve healthy kernels. Fence every publication by filesystem incarnation and active controller. Crash at admission, reply, capture, acknowledgement and commit boundaries; lost ownership/request history must never become a zero-writer baseline. |
| Monitor death and unmount | Server death, forced/lazy unmount, replacement and missing ledger state hold publication. Never silently expose backing files or remount and assume closure. Test whether abort/error behavior preserves the only raw copy; do not acknowledge complete retention when cached bytes were lost. Healthy kernels using other paths need not be killed to maintain the hold. |
| Real OpenShell | Prove installation, confinement, mount identity, actual handle/mmap lifetime, rename behavior, failure recovery and bounded resource use inside the selected OpenShell VM driver. Local FUSE tests and upstream documentation alone cannot close LD-49. |

FUSE provides an explicit abort boundary that errors pending and new requests;
this can support a held/error state after failure. It does not itself establish
recoverable counters, preserved cached bytes or automatic server restart.
[Linux FUSE connection documentation](https://docs.kernel.org/filesystems/fuse/fuse.html).

## Plan effect

Keep LD-49, LD-50 to LD-52 and LD-F12/23–27 open. The catalog prerequisite is
implemented, but runtime publication remains disabled. Continue independent
control outbox, retained direct-edit adapter and helper-contract work without
claiming a mirror integration. Complete the protected-filesystem feasibility
prerequisite before enabling replacement/removal of existing document files.

The prerequisite may conclude that no supported protected setup is available.
In that case retain the explicit hold and report the missing boundary; do not
replace it with notification counts, helper-only locks, privileged peer
inspection in research code, or routine VM recycling. This report changes no
user requirement or OpenShell/security setting and authorizes no new service.
