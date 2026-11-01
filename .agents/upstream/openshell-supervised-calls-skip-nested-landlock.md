# OpenShell: calls the supervisor carries out skip the caller's own Landlock ruleset

**Status.** Kept here, not filed upstream: filing is the user's call. Observed on babbage in the [DA-20 lab](../docs/agent-reports/babbage-da20-lab-2026-11-01.md) (2026-11-01), in a VM sandbox (guest kernel 6.12), and read in the v0.1.5 source (`crates/openshell-sandbox/src/linux/process_signal.rs`, `child_seccomp.rs`, `seccomp_notify.rs`).

**Affects.** OpenShell v0.1.5, VM driver. The mechanism is in the sandbox supervisor, so other drivers very likely behave alike; not checked.

## Summary

A workload process may add a Landlock ruleset of its own, nested in the sandbox's, to confine a part of its work further (Landlock is designed for this: an unprivileged process can restrict itself and its descendants). OpenShell's supervisor carries out some of the workload's system calls itself, answering a seccomp user notification:

- **Signals.** An allowed `kill(pid > 0, sig)` or `rt_sigqueueinfo` is delivered by the supervisor's own `pidfd_send_signal` (`mediate_process_signal`).
- **Sockets.** The supervisor makes the workload's Internet sockets and injects the descriptor (socket virtualization; `seccomp_addfd`), binding and connecting them itself.

Those operations run with the supervisor's credentials and Landlock domain, not the caller's. A nested ruleset's signal scope (ABI 6) and TCP rules (ABI 4) therefore never see them:

| Under a nested ruleset with signals scoped and TCP handled | Result in an OpenShell 0.1.5 sandbox | On a plain Linux 6.12+ kernel |
| --- | --- | --- |
| `kill(<process outside the ruleset>, SIGTERM)` | delivered | `EPERM` |
| `rt_sigqueueinfo(<outside>, sig, {si_code = SI_QUEUE})` | delivered (from the source; `si_code >= 0` is refused) | `EPERM` |
| `tgkill`, `tkill`, `rt_tgsigqueueinfo` to the same process | `EPERM` (the kernel carries them out, or the supervisor continues them) | `EPERM` |
| `bind(127.0.0.1:0)` on a TCP socket | succeeds | `EACCES` |
| `connect` to a loopback TCP server outside the ruleset | succeeds | `EACCES` |

## Consequences

A program that holds part of its work with a nested ruleset, as Sandbox Science holds a delegated agent's processes (DA-20), gets less than Landlock promises without any sign of it: the ruleset applies, its ABI is reported, and only these operations escape. In the lab a held process ended its parent's Jupyter kernel with `kill -TERM`.

## Reproduction

In a sandbox, with a Python that has `ctypes`: create a ruleset handling `LANDLOCK_ACCESS_NET_BIND_TCP` and `LANDLOCK_ACCESS_NET_CONNECT_TCP` with `scoped = LANDLOCK_SCOPE_SIGNAL`, restrict yourself, then `os.kill(os.getppid(), 0)` and `socket.socket().bind(("127.0.0.1", 0))`. Both succeed; on the host both are refused.

## Expected

The supervisor's decision should add to the kernel's checks, not replace them. For an allowed call it can answer the notification with `SECCOMP_USER_NOTIF_FLAG_CONTINUE`, so the kernel performs the call in the caller's context with the caller's LSM checks, as it already does for `tkill`, `tgkill` and `rt_tgsigqueueinfo`. Where it must perform the call itself (sockets it makes), its documentation should say that a workload's own nested Landlock rules do not apply to those operations.

## Our mitigation

- **Signals.** The held process's own seccomp filter refuses `kill` and `rt_sigqueueinfo` with `EPERM` (and descriptor owners: `F_SETOWN`, `F_SETOWN_EX`, `F_SETSIG`, `FIOSETOWN`, `SIOCSPGRP`). A filter's `ERRNO` takes precedence over the supervisor's `USER_NOTIF`, so the supervisor never sees them. A preloaded shim (`shim/signal_self.c`) gives the held programs `kill` back through `tgkill`, which Landlock checks. The executor's probe checks that no signal leaves a ruleset before it holds any run.
- **TCP.** Not mitigated: recorded in DA-20's *Not covered* list, and reported as `tcp_held: false` in the executor's `hello`.
