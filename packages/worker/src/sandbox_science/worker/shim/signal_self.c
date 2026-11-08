// SPDX-License-Identifier: MIT
/*
 * kill, killpg and sigqueue for a delegated run's processes (DA-20).
 *
 * A held run's seccomp filter (sandbox_confine.py) refuses kill and
 * rt_sigqueueinfo with EPERM whatever they name, since a sandbox supervisor may
 * carry those out for the caller outside its Landlock domain (OpenShell 0.1.5
 * does), and the domain's signal scope would then never see them. This
 * library, loaded with LD_PRELOAD in a held run only, sends the same signals
 * through the thread-directed calls, which the kernel checks against the
 * caller's domain: a process of the run reaches its own processes and nothing
 * outside, which gets EPERM.
 *
 * - kill(pid > 0) is tgkill(pid, pid): the signal goes to the process's main
 *   thread rather than to any thread that does not block it.
 * - kill(0), kill(-pgid) and killpg signal each member of the group that /proc
 *   lists, the caller last; the list is read again until it shows no new
 *   member, so a process forked meanwhile is not missed. kill(-1) signals every
 *   process but the caller and init. Members outside the domain are refused by
 *   the kernel, one by one, as kill() would refuse them.
 * - sigqueue is rt_tgsigqueueinfo(pid, pid).
 *
 * Every function makes only system calls, with buffers on the stack, so each is
 * async-signal-safe like the call it replaces (timeout(1) calls kill(0) in its
 * signal handler). A program that makes the system call itself, or is linked
 * statically, does not get this library: its kill() is EPERM.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdint.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>

#ifndef SANDBOX_SCIENCE_SIGNAL_SOURCE_SHA256
#error "build the signal shim with its source SHA-256"
#endif

#define SIGNAL_LIMIT 65 /* the kernel's _NSIG + 1: valid signals are 0 to 64 */
#define TRACKED 256     /* processes remembered across passes of one group signal */
#define PASSES 8        /* passes over /proc for one group signal, at most */

const char *sandbox_science_signal_source_sha256(void)
{
    return SANDBOX_SCIENCE_SIGNAL_SOURCE_SHA256;
}

static int fail(int error)
{
    errno = error;
    return -1;
}

static int tgkill_process(pid_t pid, int sig)
{
    if (syscall(SYS_tgkill, (long)pid, (long)pid, (long)sig) == 0) {
        return 0;
    }
    return errno;
}

struct linux_dirent64 {
    uint64_t d_ino;
    int64_t d_off;
    unsigned short d_reclen;
    unsigned char d_type;
    char d_name[];
};

static pid_t parse_pid(const char *name)
{
    long value = 0;
    if (*name == '\0') {
        return 0;
    }
    for (; *name != '\0'; name++) {
        if (*name < '0' || *name > '9' || value > INT_MAX / 10) {
            return 0;
        }
        value = value * 10 + (*name - '0');
    }
    return value > INT_MAX ? 0 : (pid_t)value;
}

/* The process group of pid, from /proc/<pid>/stat, or -1 when it is gone. */
static pid_t group_of(int proc, const char *name)
{
    char path[32];
    char stat[512];
    size_t length = strlen(name);
    if (length > sizeof(path) - 6) {
        return -1;
    }
    memcpy(path, name, length);
    memcpy(path + length, "/stat", 6);
    int descriptor = (int)syscall(SYS_openat, (long)proc, path, (long)(O_RDONLY | O_CLOEXEC));
    if (descriptor < 0) {
        return -1;
    }
    ssize_t got = syscall(SYS_read, (long)descriptor, stat, (long)(sizeof(stat) - 1));
    syscall(SYS_close, (long)descriptor);
    if (got <= 0) {
        return -1;
    }
    stat[got] = '\0';
    /* "pid (comm) state ppid pgrp ...": comm may hold anything, so start at the last ')'. */
    char *end = NULL;
    for (char *at = stat; *at != '\0'; at++) {
        if (*at == ')') {
            end = at;
        }
    }
    if (end == NULL) {
        return -1;
    }
    int field = 0;
    long value = 0;
    int digits = 0;
    for (char *at = end + 1; *at != '\0'; at++) {
        if (*at == ' ') {
            if (field == 3 && digits) {
                return (pid_t)value;
            }
            field++;
            continue;
        }
        if (field == 3) {
            if (*at < '0' || *at > '9' || value > INT_MAX / 10) {
                return -1;
            }
            value = value * 10 + (*at - '0');
            digits = 1;
        }
    }
    return -1;
}

/*
 * Signal each process in group (or, with every, each but the caller and init),
 * as kill(-group) or kill(-1) would. Returns 0 or an errno value.
 */
static int signal_many(pid_t group, int every, int sig)
{
    pid_t self = (pid_t)syscall(SYS_getpid);
    pid_t tracked[TRACKED];
    size_t count = 0;
    int members = 0, delivered = 0, error = ESRCH, last = 0, include_self = 0;
    for (int pass = 0; pass < PASSES; pass++) {
        int proc = (int)syscall(
            SYS_openat, (long)AT_FDCWD, "/proc", (long)(O_RDONLY | O_DIRECTORY | O_CLOEXEC));
        if (proc < 0) {
            return errno;
        }
        int added = 0;
        char buffer[2048] __attribute__((aligned(8)));
        for (;;) {
            long got = syscall(SYS_getdents64, (long)proc, buffer, (long)sizeof(buffer));
            if (got <= 0) {
                break;
            }
            for (long offset = 0; offset < got;) {
                struct linux_dirent64 *entry = (struct linux_dirent64 *)(buffer + offset);
                offset += entry->d_reclen;
                pid_t pid = parse_pid(entry->d_name);
                if (pid <= 0) {
                    continue;
                }
                if (every ? pid == 1 : group_of(proc, entry->d_name) != group) {
                    continue;
                }
                if (pid == self) {
                    include_self = !every;
                    continue;
                }
                int seen = 0;
                for (size_t index = 0; index < count && !seen; index++) {
                    seen = tracked[index] == pid;
                }
                if (seen) {
                    continue;
                }
                if (count < TRACKED) {
                    tracked[count++] = pid;
                    added = 1;
                }
                int result = tgkill_process(pid, sig);
                if (result == ESRCH) {
                    continue; /* gone since the listing, as it would be to kill() */
                }
                members++;
                if (result == 0) {
                    delivered++;
                } else {
                    error = result;
                }
                if (result != EPERM) {
                    last = result;
                }
            }
        }
        syscall(SYS_close, (long)proc);
        if (!added || count >= TRACKED) {
            break; /* nothing new; or too many to tell new ones from those signalled */
        }
    }
    if (include_self) {
        int result = tgkill_process(self, sig);
        members++;
        if (result == 0) {
            delivered++;
        } else {
            error = result;
        }
    }
    if (every) {
        /* kill(-1) ignores refusals: the last other answer, or ESRCH if it found no one. */
        return members == 0 ? ESRCH : last;
    }
    return delivered > 0 ? 0 : (members == 0 ? ESRCH : error);
}

static int send_signal(pid_t pid, int sig)
{
    if (sig < 0 || sig >= SIGNAL_LIMIT) {
        return fail(EINVAL);
    }
    if (pid > 0) {
        int result = tgkill_process(pid, sig);
        return result == 0 ? 0 : fail(result);
    }
    if (pid == INT_MIN) {
        return fail(ESRCH);
    }
    int result;
    if (pid == 0) {
        result = signal_many((pid_t)syscall(SYS_getpgid, 0L), 0, sig);
    } else if (pid == -1) {
        result = signal_many(0, 1, sig);
    } else {
        result = signal_many(-pid, 0, sig);
    }
    return result == 0 ? 0 : fail(result);
}

int kill(pid_t pid, int sig)
{
    return send_signal(pid, sig);
}

int killpg(pid_t group, int sig)
{
    if (group < 0) {
        return fail(EINVAL);
    }
    return send_signal(-group, sig);
}

int sigqueue(pid_t pid, int sig, const union sigval value)
{
    siginfo_t info;
    memset(&info, 0, sizeof(info));
    info.si_signo = sig;
    info.si_code = SI_QUEUE;
    info.si_pid = (pid_t)syscall(SYS_getpid);
    info.si_uid = (uid_t)syscall(SYS_getuid);
    info.si_value = value;
    if (syscall(SYS_rt_tgsigqueueinfo, (long)pid, (long)pid, (long)sig, &info) == 0) {
        return 0;
    }
    return -1;
}
