# libzmq: a refused pthread_setschedparam aborts the process

**Status.** Kept here, not filed upstream: filing is the user's call. Observed on babbage in [lab 4](../docs/agent-reports/babbage-openshell-lab-4-2026-10-11.md) (2026-10-11) with libzmq 4.3.5 (pyzmq 27.2.0 from conda-forge), inside an OpenShell sandbox whose seccomp filter refuses the call ([OpenShell description](openshell-seccomp-thread-and-group-calls.md)).

## Summary

Every thread libzmq starts sets its own scheduling parameters, in `zmq::thread_t::applySchedulingParameters` (`src/thread.cpp`). It does this even when the application set no scheduling options, writing back the values it has just read. If `pthread_setschedparam` fails, `posix_assert` aborts the whole process with `Operation not permitted (src/thread.cpp:315)`. The code already tolerates `ENOSYS` on FreeBSD, but not `EPERM`. Seccomp sandboxes, some container runtimes and gVisor-like kernels return EPERM for this call. In them, any ZeroMQ program dies as soon as it creates a context: Jupyter kernels and clients, and anything built on pyzmq.

## Reproduction

In an environment where `sched_setscheduler` on a thread ID is refused, for example an OpenShell v0.1.3 sandbox:

```bash
python -c "import zmq; zmq.Context().socket(zmq.PUSH)"
# Operation not permitted (src/thread.cpp:315)
# (process aborted)
```

## Expected

Without `ZMQ_THREAD_SCHED_POLICY` or `ZMQ_THREAD_PRIORITY` set, libzmq has no reason to change anything. It should skip the call, or ignore its failure. When the application did ask for a policy or priority, failing loudly is reasonable, but returning an error from context or socket creation would be better than aborting.

## Suggested fix

In `applySchedulingParameters`, return early when both the policy and the priority are at their defaults. Otherwise treat `EPERM` like `ENOSYS`: skip it, perhaps with a debug message.

## Our mitigation

The worker service preloads a small shim in sandboxes. The shim makes `pthread_setschedparam` on the calling thread use `sched_setscheduler(0, …)`, which the sandbox allows (docs/WORKER.md).
