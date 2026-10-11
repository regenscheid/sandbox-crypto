# OpenShell: seccomp refuses scheduler calls on a thread and signals to a process group

**Status.** Kept here, not filed upstream: filing is the user's call. Observed on babbage in [lab 4](../docs/agent-reports/babbage-openshell-lab-4-2026-10-11.md) (2026-10-11), in a VM sandbox (guest kernel 6.12.76, `Seccomp: 2`, five filters, `NoNewPrivs: 1`, no capabilities). The filter's source was not in the inspected snapshot, so the description rests on the observed results.

**Affects.** OpenShell v0.1.3 (tag commit `e1f3c82caa3e`), VM driver. Other drivers probably apply the same filter; not checked.

## Summary

Inside a sandbox, some calls fail with EPERM although the Linux kernel allows them to an unprivileged process acting on itself:

| Call | Result |
| --- | --- |
| `sched_setscheduler(0, SCHED_OTHER, {0})`, the calling thread by pid 0 | allowed |
| `sched_setparam(0, {0})` | allowed |
| `pthread_setschedparam(pthread_self(), SCHED_OTHER, {0})`, which glibc makes `sched_setscheduler(<own thread id>, …)` | **EPERM** |
| `setpriority(PRIO_PROCESS, 0, 0)`, leaving the nice value unchanged | **EPERM** |
| `kill(pid, SIGTERM)` to the caller's own child | allowed |
| `kill(-pgid, SIGTERM)` and `killpg(pgid, SIGTERM)` to the caller's own child's process group | **EPERM** |

## Consequences

- **No ZeroMQ program runs, so no Jupyter kernel either.** libzmq calls `pthread_setschedparam` on every thread it starts and aborts when it fails ([libzmq description](libzmq-aborts-on-eperm-from-pthread-setschedparam.md)). Every Jupyter kernel and client is affected, Python, R and Sage alike.
- **Shells and supervisors cannot stop job groups.** That covers bash job control, `timeout`, test runners and our executor's cancellation, which signal process groups. Kernel interrupts still work only because jupyter_client falls back to signalling one process.

## Reproduction

In any sandbox with Python:

```python
import ctypes, ctypes.util, os, signal, subprocess, time

libc = ctypes.CDLL(ctypes.util.find_library("c"))


class P(ctypes.Structure):
    _fields_ = [("prio", ctypes.c_int)]


policy, param = ctypes.c_int(), P()
me = ctypes.c_ulong(libc.pthread_self())
libc.pthread_getschedparam(me, ctypes.byref(policy), ctypes.byref(param))
print(libc.pthread_setschedparam(me, policy, ctypes.byref(param)))  # 1 (EPERM)
child = subprocess.Popen(["sleep", "100"], start_new_session=True)
time.sleep(0.2)
os.killpg(child.pid, signal.SIGTERM)  # PermissionError
```

With ZeroMQ installed: `python -c "import zmq; zmq.Context().socket(zmq.PUSH)"` aborts with `Operation not permitted (src/thread.cpp:315)`.

## Expected

A process may change the scheduling of its own threads, within the limits the kernel already applies to unprivileged processes. It may also signal its own children's process groups. Both act only on processes the sandbox already controls. If they are refused on purpose, OpenShell's documentation should say so, given what it breaks.

## Suggested fix

- **Scheduling.** Allow `sched_setscheduler`, `sched_setparam` and `setpriority` when the target is a thread of the calling process. That is the case seccomp can check: pid 0 or the caller's own thread group. If argument checks cannot express it, allow them outright for non-real-time policies, since the kernel already refuses real-time ones without CAP_SYS_NICE.
- **Signals.** Allow `kill` with a negative pid when the group belongs to the caller's session or descendants.

## Our mitigation

The worker service handles both (docs/WORKER.md):
- it preloads a shim that turns `pthread_setschedparam` on the calling thread into the allowed pid-0 call;
- the executor signals process-group members one at a time when `killpg` is refused.
