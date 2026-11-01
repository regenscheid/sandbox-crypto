// SPDX-License-Identifier: MIT
/*
 * pthread_setschedparam for sandboxes whose seccomp filter refuses scheduler
 * calls that name a thread, as OpenShell v0.1.5's does.
 *
 * libzmq sets every thread it starts to its current scheduling parameters with
 * pthread_setschedparam(pthread_self(), ...). glibc turns that into
 * sched_setscheduler(<thread id>, ...), which the filter refuses with EPERM,
 * and libzmq then aborts: no Jupyter kernel can start. The same call with pid 0
 * (the calling thread) is allowed and has the same effect, so this library,
 * loaded with LD_PRELOAD, makes that call for the calling thread. A call for
 * another thread fails with EPERM, as it would without the library.
 *
 * It uses only pthread_equal, pthread_self and syscall, which every glibc
 * provides, so one build runs on older and newer distributions alike.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef SANDBOX_SCIENCE_SCHED_SOURCE_SHA256
#error "build the scheduler shim with its source SHA-256"
#endif

const char *sandbox_science_sched_source_sha256(void)
{
    return SANDBOX_SCIENCE_SCHED_SOURCE_SHA256;
}

int pthread_setschedparam(pthread_t thread, int policy, const struct sched_param *param)
{
    if (!pthread_equal(thread, pthread_self())) {
        return EPERM;
    }
    if (syscall(SYS_sched_setscheduler, 0, policy, param) == -1) {
        return errno;
    }
    return 0;
}
