# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""DA-20 on real OpenShell: a delegated run's processes held to its write area (2026-11-01).

Scripted, no model. Run with the virtualenv of the fixed checkout against a root that
``model_lab.py up`` brought up (its worker, on the lab's own OpenShell gateway, VM driver):

    .venv/bin/python -I .agents/labs/da20_probes.py LAB [PART ...]

Parts (default: all, in this order): ``seed``, ``hello``, ``shell``, ``cell``, ``job``,
``network``, ``process``, ``provision``, ``parent``. Each sends worker API requests as the
harness does, in project ``modellab``'s environment, session ``held``: a parent run's
requests name no write area; a child's name ``work/dlg-da20a``; a sibling's area is
``work/dlg-da20b``. Each part's requests and answers are written as the root's evidence
``da20-<part>`` (``model_lab.py cleanup`` exports it with a SHA-256 manifest), and a one-line
summary is printed. Nothing here judges: the report reads the outputs.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION

spec = importlib.util.spec_from_file_location("model_lab", Path(__file__).with_name("model_lab.py"))
assert spec is not None
assert spec.loader is not None
model_lab = importlib.util.module_from_spec(spec)
sys.modules["model_lab"] = model_lab
spec.loader.exec_module(model_lab)

PROJECT = "modellab"
SESSION = "held"
AREA = "work/dlg-da20a"
SIBLING = "work/dlg-da20b"
CHILD_RUN = "r-da20a"
STAMP = time.strftime("%H%M%S")
TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "lost"}


class Probe:
    def __init__(self, root: Path) -> None:
        self.lab = model_lab.ModelLab(root)
        self.port = self.lab.ports["worker"]
        self.token = (self.lab.path / "instance/tokens/harness-worker.token").read_text().strip()
        (environment,) = self.lab.read(
            "SELECT environment_id FROM sessions WHERE project_id=? AND session_id=?",
            (PROJECT, SESSION),
        )
        self.environment = environment[0]
        self.seq = 0

    def context(self, area: str | None = None, actor: str = "agent") -> dict[str, str]:
        found = {
            "project_id": PROJECT,
            "session_id": SESSION,
            "environment_id": self.environment,
            "actor": actor,
        }
        return found if area is None else {**found, "write_area": area}

    def call(self, method: str, path: str, body: Any = None, timeout: float = 120):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.token,
                PROTOCOL_HEADER: str(PROTOCOL_VERSION),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            text = error.read().decode(errors="replace")
            try:
                return error.code, json.loads(text)
            except ValueError:
                return error.code, {"text": text[:2000]}

    def op(self, name: str) -> str:
        self.seq += 1
        return f"da20-{STAMP}-{self.seq:02d}-{name}"[:120]

    def submit(self, name: str, area: str | None, **request: Any) -> dict[str, Any]:
        body = {"operation_id": self.op(name), "context": self.context(area), **request}
        status, found = self.call("POST", "/v1/executions", body)
        if status >= 400:
            return {"http": status, "refused": found}
        return found

    def wait(self, execution: dict[str, Any], seconds: float = 600) -> dict[str, Any]:
        if "execution_id" not in execution:
            return execution
        end = time.monotonic() + seconds
        while True:
            status, found = self.call("GET", f"/v1/executions/{execution['execution_id']}")
            assert status == 200, found
            if found["state"] in TERMINAL or time.monotonic() > end:
                return found
            time.sleep(0.5)

    def run(self, name: str, area: str | None, **request: Any) -> dict[str, Any]:
        return self.wait(self.submit(name, area, **request))

    def wait_running(self, execution: dict[str, Any], seconds: float = 120) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            _, found = self.call("GET", f"/v1/executions/{execution['execution_id']}")
            if found.get("state") == "running":
                return
            if found.get("state") in TERMINAL:
                raise RuntimeError(f"ended before running: {json.dumps(found)[:800]}")
            time.sleep(0.5)
        raise TimeoutError("never ran")

    def write(self, path: str, text: str, area: str | None = None) -> tuple[int, Any]:
        body: dict[str, Any] = {
            "context": self.context(area),
            "path": path,
            "content_b64": base64.b64encode(text.encode()).decode(),
        }
        if path.startswith("shared/"):  # the shared area's guard (expected digest or new)
            status, found = self.call(
                "POST",
                "/v1/files/read",
                {"context": self.context(), "path": path, "offset": 0, "length": 0},
            )
            if status == 200:
                body["expected_sha256"] = found["sha256"]
            else:
                body["create_only"] = True
        return self.call("POST", "/v1/files/write", body)

    def read(self, path: str) -> str | None:
        status, found = self.call(
            "POST",
            "/v1/files/read",
            {"context": self.context(), "path": path, "offset": 0, "length": 65536},
        )
        if status != 200:
            return None
        return base64.b64decode(found["content_b64"]).decode(errors="replace")

    def save(self, part: str, value: Any) -> None:
        self.lab.evidence(f"da20-{part}", value)


def brief(execution: dict[str, Any]) -> dict[str, Any]:
    """The parts of an ended execution the report reads."""
    if "execution_id" not in execution:
        return execution
    result = execution.get("result") or {}
    return {
        "execution_id": execution["execution_id"],
        "kind": execution.get("kind"),
        "state": execution["state"],
        "exit_code": result.get("exit_code"),
        "stdout": ((result.get("stdout") or {}).get("text") or "")[-12000:],
        "stderr": ((result.get("stderr") or {}).get("text") or "")[-4000:],
        "outputs": result.get("outputs"),
        "error": result.get("error"),
    }


SEEDED = {
    "sources/a.md": "original parent file\n",
    "inputs/data.csv": "x,y\n1,2\n",
    f"{SIBLING}/notes.md": "sibling original\n",
    "shared/readme.txt": "shared original\n",
}


# The child's shell. Each attempt prints WROTE or REFUSED with the error's last words.
SHELL = r"""
echo "cwd=$(pwd)"
echo "RUN_WORK=${RUN_WORK:-unset}"
echo "TMPDIR=${TMPDIR:-unset}"
echo "SESSION_WORKSPACE=$SESSION_WORKSPACE PROJECT_SHARED=${PROJECT_SHARED:-unset}"
grep -E '^(NoNewPrivs|Seccomp|Seccomp_filters)' /proc/self/status | tr '\t' ' '
w() { echo "$ROLE" > "$1"; }
attempt() { local name=$1; shift; local out; if out=$( { "$@"; } 2>&1 ); then echo "WROTE $name"; else echo "REFUSED $name: ${out##*: }"; fi; }
attempt area-notes w notes.md
attempt area-relative-subdir sh -c 'mkdir -p results && echo x > results/r.txt'
attempt tmpdir w "${TMPDIR:-/tmp}/t-$ROLE"
attempt parent-file w "$SESSION_WORKSPACE/sources/a.md"
attempt parent-new-file w "$SESSION_WORKSPACE/sources/new-$ROLE.md"
attempt sibling-area w "$SESSION_WORKSPACE/work/dlg-da20b/notes.md"
attempt inputs w "$SESSION_WORKSPACE/inputs/data.csv"
attempt shared w "$PROJECT_SHARED/x-$ROLE.txt"
attempt executor-dir w "$SESSION_WORKSPACE/../../.executor/injected-$ROLE.py"
PURELIB=$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
echo "purelib=$PURELIB python3=$(command -v python3)"
attempt package-layer w "$PURELIB/da20-$ROLE.pth"
attempt prefix-bin w "$(dirname "$(command -v python3)")/da20-$ROLE"
attempt home w "$HOME/da20-$ROLE"
attempt rm-parent rm "$SESSION_WORKSPACE/sources/a.md"
attempt mv-out mv notes.md "$SESSION_WORKSPACE/sources/moved-$ROLE.md"
attempt symlink-write sh -c "ln -sfn '$SESSION_WORKSPACE/sources' link-$ROLE && echo $ROLE > link-$ROLE/a.md"
echo "read inputs: $(cat "$SESSION_WORKSPACE/inputs/data.csv" | tr '\n' ' ')"
echo "read shared: $(cat "$PROJECT_SHARED/readme.txt" 2>&1)"
echo "read parent: $(cat "$SESSION_WORKSPACE/sources/a.md" 2>&1)"
echo done
"""


CELL = r"""
import json, os, socket, signal
from pathlib import Path
session = Path(os.environ["SESSION_WORKSPACE"])
shared = Path(os.environ.get("PROJECT_SHARED", str(session.parent.parent / "shared")))
found = {"cwd": os.getcwd(), "RUN_WORK": os.environ.get("RUN_WORK"), "wrote": [], "refused": {}}
status = dict(line.split(":\t", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":\t" in line)
found["seccomp_filters"] = status.get("Seccomp_filters", "").strip()
Path("cell.txt").write_text("mine")
found["wrote"].append("area cell.txt")
for name, path in [("parent", session / "sources/a.md"), ("input", session / "inputs/data.csv"),
                   ("sibling", session / "work/dlg-da20b/notes.md"), ("shared", shared / "x-cell.txt"),
                   ("executor", session.parent.parent / ".executor/injected-cell.py")]:
    try:
        path.write_text("child cell")
        found["wrote"].append(name)
    except OSError as error:
        found["refused"][name] = f"{error.errno} {error.strerror}"
own = None
try:
    import ipykernel.connect
    own = ipykernel.connect.get_connection_info(unpack=True)["ip"]
except Exception as error:
    found["own_error"] = repr(error)
found["own_socket_prefix"] = own
tried, connected = 0, []
for line in Path("/proc/net/unix").read_text().splitlines()[1:]:
    parts = line.split()
    name = parts[-1] if len(parts) > 7 else ""
    if name.startswith("@ss-kernel-") and own and not name.startswith(own):
        tried += 1
        with socket.socket(socket.AF_UNIX) as client:
            try:
                client.connect("\0" + name[1:])
                connected.append(name)
            except OSError as error:
                found.setdefault("connect_errors", set()).add(error.errno)
found["other_kernel_sockets_tried"] = tried
found["connected"] = connected
found["connect_errors"] = sorted(found.get("connect_errors", ()))
for sig in (0, signal.SIGCONT):
    try:
        os.kill(PARENT, sig)
        found[f"kill {int(sig)}"] = "delivered"
    except OSError as error:
        found[f"kill {int(sig)}"] = f"{error.errno} {error.strerror}"
print(json.dumps(found))
"""


NETWORK = r"""
import ctypes, json, os, socket, subprocess, urllib.parse
libc = ctypes.CDLL(None, use_errno=True)
found = {}
proxies = {k: v for k, v in os.environ.items() if k.lower() in ("http_proxy", "https_proxy", "all_proxy")}
found["proxy_variables"] = sorted(proxies)
ports = sorted({urllib.parse.urlsplit(v if "://" in v else "http://" + v).port for v in proxies.values()} - {None})
found["proxy_ports"] = ports
def attempt(name, call):
    try:
        found[name] = call()
    except OSError as error:
        found[name] = f"errno {error.errno} {error.strerror}"
import urllib.request, urllib.error
def curl(url):  # urllib honours the proxy variables, as curl and pip do
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return f"http {response.status}"
    except urllib.error.HTTPError as error:
        return f"http {error.code}"
    except Exception as error:
        return f"error {type(error).__name__}: {str(error)[:160]}"
found["https github.com via proxy"] = curl("https://github.com/")
index = os.environ.get("PIP_INDEX_URL") or os.environ.get("UV_INDEX_URL") or ""
found["package index variable set"] = bool(index)
if index:
    found["package cache via proxy"] = curl(index.rstrip("/") + "/pip/")
    cache = urllib.parse.urlsplit(index)
    def direct():
        with socket.create_connection((cache.hostname, cache.port or 80), timeout=10):
            return "connected"
    attempt("package cache direct TCP (not through the proxy)", direct)
def bind():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        return f"bound {s.getsockname()[1]}"
attempt("tcp bind+listen 127.0.0.1", bind)
def listen_unbound():
    with socket.socket() as s:
        s.listen()
        return f"listening on ephemeral {s.getsockname()[1]}"
attempt("tcp listen() unbound", listen_unbound)
def sock(family, kind, proto):
    def make():
        with socket.socket(family, kind, proto):
            return "created"
    return make
attempt("mptcp socket", sock(socket.AF_INET, socket.SOCK_STREAM, 262))
attempt("mptcp6 socket", sock(socket.AF_INET6, socket.SOCK_STREAM, 262))
attempt("sctp socket", sock(socket.AF_INET, socket.SOCK_STREAM, 132))
smc = libc.socket(43, socket.SOCK_STREAM, 0)
found["af_smc socket"] = f"errno {ctypes.get_errno()}" if smc < 0 else "created"
params = ctypes.create_string_buffer(120)
ring = libc.syscall(ctypes.c_long(IO_URING_SETUP), ctypes.c_uint(4), params)
found["io_uring_setup"] = f"errno {ctypes.get_errno()}" if ring < 0 else "created"
attempt("udp socket", sock(socket.AF_INET, socket.SOCK_DGRAM, 0))
print(json.dumps(found))
"""


REACH = r"""
import ctypes, fcntl, json, os, resource, signal, time
found = {"pid": os.getpid()}
def attempt(name, call):
    try:
        result = call()
        found[name] = "done" if result is None else f"done {result}"
    except OSError as error:
        found[name] = f"errno {error.errno} {error.strerror}"
libc = ctypes.CDLL(None, use_errno=True)
def checked(result):
    if result != 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
for label, pid in (("kernel", KERNEL), ("executor", EXECUTOR)):
    attempt(f"prlimit NOFILE {label}", lambda: resource.prlimit(pid, resource.RLIMIT_NOFILE, (3, 3)))
    attempt(f"prlimit CPU {label}", lambda: resource.prlimit(pid, resource.RLIMIT_CPU, (1, 1)))
    attempt(f"setpriority {label}", lambda: os.setpriority(os.PRIO_PROCESS, pid, 19))
    attempt(f"ioprio_set {label}", lambda: checked(libc.syscall(IOPRIO_SET, 1, pid, (3 << 13))))
    attempt(f"ptrace attach {label}", lambda: checked(libc.ptrace(16, pid, 0, 0)))
    attempt(f"kill 0 {label}", lambda: os.kill(pid, 0))
    attempt(f"kill SIGCONT {label}", lambda: os.kill(pid, signal.SIGCONT))
    attempt(f"tgkill 0 {label}", lambda: checked(libc.syscall(TGKILL, pid, pid, 0)))
    attempt(f"rt_sigqueueinfo 0 {label}", lambda: checked(libc.syscall(RT_SIGQUEUEINFO, pid, 0, ctypes.create_string_buffer(128))))
    attempt(f"sched_setaffinity {label}", lambda: os.sched_setaffinity(pid, {0}))
attempt("prlimit self", lambda: resource.prlimit(0, resource.RLIMIT_NOFILE))
lock = os.open(LOCKED, os.O_RDONLY)
attempt("flock LOCK_EX parent file (read-only fd)", lambda: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB))
attempt("lockf LOCK_SH parent file", lambda: fcntl.lockf(lock, fcntl.LOCK_SH | fcntl.LOCK_NB))
attempt("F_SETLEASE parent file", lambda: fcntl.fcntl(lock, fcntl.F_SETLEASE, fcntl.F_RDLCK))
open("reach.json", "w").write(json.dumps(found) + "\n")
print(json.dumps(found), flush=True)
time.sleep(HOLD)
"""


def ensure_environment(p: Probe) -> dict[str, Any]:
    """The session's environment, created as the harness's ``ensure_environment`` does
    when the worker has none yet (the first run of a session creates it)."""
    from sandbox_science.orchestrator.projects import ProjectStore

    status, found = p.call("GET", f"/v1/environments/{p.environment}")
    if status == 200 and found.get("state") != "deleted":
        return {"existing": found.get("state")}
    store = ProjectStore(p.lab.path / "instance/control", readonly=True)
    try:
        project = store.get(PROJECT)
    finally:
        store.close()
    body = {
        "environment_id": p.environment,
        "project_id": PROJECT,
        "profile": project.profile,
        "policy": project.policy,
    }
    status, found = p.call("POST", "/v1/environments", body, timeout=900)
    p.lab.remember_sandboxes()
    return {"created": status, "state": found.get("state"), "error": found.get("error")}


def part_seed(p: Probe) -> None:
    ensured = ensure_environment(p)
    print("environment", ensured)
    value = {name: p.write(name, text) for name, text in SEEDED.items()}
    p.save("seed", {"environment": p.environment, "ensured": ensured, "writes": value})
    print("seed", {k: v[0] for k, v in value.items()})


def part_hello(p: Probe) -> None:
    """What the executor's hello reports, reproduced in the sandbox as the executor does
    (``sandbox_confine.abi()`` and ``--probe``), and the kernel and filters seen."""
    script = r"""
set -u
uname -r
grep -E '^(NoNewPrivs|Seccomp|Seccomp_filters)' /proc/self/status | tr '\t' ' '
DIR=/sandbox/.sandbox-science  # the in-sandbox files beside the daemon and executor
ls "$DIR" | tr '\n' ' '; echo
echo "executor_dir_found=$([ -f "$DIR/sandbox_confine.py" ] && echo yes || echo no)"
cd "$DIR" && python3 -I -c 'import sys; sys.path.insert(0, "."); import sandbox_confine as c; print("abi", c.abi(), "protocol", c.PROTOCOL, "required", c.REQUIRED_ABI)'
python3 -I -S "$DIR/sandbox_confine.py" --probe; echo "probe_exit=$?"
"""
    parent = brief(p.run("hello-parent", None, kind="shell", script=script))
    child = brief(
        p.run(
            "hello-child",
            AREA,
            kind="shell",
            script="grep -E '^(NoNewPrivs|Seccomp|Seccomp_filters)' /proc/self/status | tr '\\t' ' '; pwd",
        )
    )
    p.save("hello", {"parent": parent, "child": child})
    print("hello parent:", parent["state"], parent["stdout"], parent["stderr"][-400:])
    print("hello child:", child["state"], child["stdout"], child.get("error"))


def part_shell(p: Probe) -> None:
    child = brief(p.run("child-shell", AREA, kind="shell", script=f"ROLE=child\n{SHELL}"))
    files = {name: p.read(name) for name in SEEDED}
    p.save("shell", {"child": child, "files_after": files})
    print("child shell:", child["state"])
    print(child["stdout"])
    print("files after:", files)


def part_cell(p: Probe) -> None:
    parent = brief(
        p.run("parent-cell", None, kind="cell", code="import os; (os.getpid(), os.getppid())")
    )
    text = parent["outputs"][0]["data"]["text/plain"]
    kernel, executor = json.loads(text.replace("(", "[").replace(")", "]"))
    code = f"PARENT = {kernel}\n" + CELL
    first = brief(p.run("child-cell-1", AREA, kind="cell", code=code, run=CHILD_RUN))
    status, restart = p.call(
        "POST",
        "/v1/kernels/restart",
        {"context": p.context(AREA), "run": CHILD_RUN, "language": "python"},
    )
    second = brief(p.run("child-cell-2", AREA, kind="cell", code=code, run=CHILD_RUN))
    bare = brief(p.run("child-cell-bare", None, kind="cell", code="1", run=CHILD_RUN))
    no_run = brief(p.run("child-cell-norun", AREA, kind="cell", code="1"))
    status_bare, restart_bare = p.call(
        "POST",
        "/v1/kernels/restart",
        {"context": p.context(), "run": CHILD_RUN, "language": "python"},
    )
    again = brief(p.run("parent-cell-again", None, kind="cell", code="import os; os.getpid()"))
    files = {name: p.read(name) for name in SEEDED}
    value = {
        "parent_kernel_pid": kernel,
        "executor_pid": executor,
        "first": first,
        "restart": {"http": status, "body": restart},
        "second": second,
        "cell_without_area_on_held_kernel": bare,
        "cell_with_area_without_run": no_run,
        "restart_without_area": {"http": status_bare, "body": restart_bare},
        "parent_again": again,
        "files_after": files,
    }
    p.save("cell", value)
    print("parent kernel", kernel, "executor", executor)
    for name in ("first", "second"):
        print(name, value[name]["state"], value[name]["stdout"], value[name]["error"])
    print("restart", status, json.dumps(restart)[:300])
    print("bare", bare["state"], bare["error"])
    print("no run", no_run["state"], no_run["error"])
    print("restart bare", status_bare, json.dumps(restart_bare)[:300])
    print("parent again", again["state"], again["outputs"])
    print("files after:", files)


def part_job(p: Probe) -> None:
    script = f"ROLE=childjob\n{SHELL}\nsleep 5\necho job-finished"
    started = p.submit(
        "child-job", AREA, kind="job", script=script,
        purpose="DA-20 lab: a held background job", expected_progress="none",
    )  # fmt: skip
    ended = brief(p.wait(started, 300))
    status, listed = p.call("GET", f"/v1/jobs/{started.get('execution_id', 'none')}")
    files = {name: p.read(name) for name in SEEDED}
    p.save(
        "job",
        {"child_job": ended, "job_status": {"http": status, "body": listed}, "files_after": files},
    )
    print("child job:", ended["state"], ended["exit_code"])
    print(ended["stdout"])
    print("files after:", files)


def part_network(p: Probe) -> None:
    number = 425  # io_uring_setup on x86_64 and aarch64
    code = f"IO_URING_SETUP = {number}\n" + NETWORK
    script = f"python3 - <<'PY'\n{code}\nPY"
    child = brief(p.run("child-network", AREA, kind="shell", script=script, timeout_seconds=180))
    parent = brief(p.run("parent-network", None, kind="shell", script=script, timeout_seconds=180))
    p.save("network", {"child": child, "parent": parent})
    for name, value in (("child", child), ("parent", parent)):
        print(name, value["state"], value["stdout"] or value["stderr"][-800:])


def part_process(p: Probe) -> None:
    parent = brief(
        p.run("parent-pids", None, kind="cell", code="import os; (os.getpid(), os.getppid())")
    )
    text = parent["outputs"][0]["data"]["text/plain"]
    kernel, executor = json.loads(text.replace("(", "[").replace(")", "]"))
    probe = brief(p.run("arch", None, kind="shell", script="uname -m"))
    machine = probe["stdout"].strip()
    number, tgkill, sigqueue = {"x86_64": (251, 234, 129), "aarch64": (30, 131, 138)}[machine]
    code = (
        f"KERNEL, EXECUTOR, IOPRIO_SET, HOLD = {kernel}, {executor}, {number}, 60\n"
        f"TGKILL, RT_SIGQUEUEINFO = {tgkill}, {sigqueue}\n"
        # The job runs in its area: the parent's file is reached by its session path.
        "LOCKED = os.path.join(os.environ['SESSION_WORKSPACE'], 'sources/a.md')\n"
    )
    code = "import os\n" + code + REACH
    started = p.submit(
        "child-reach", AREA, kind="job", script=f"python3 - <<'PY'\n{code}\nPY",
        purpose="DA-20 lab: process attacks on the parent", expected_progress="none",
    )  # fmt: skip
    p.wait_running(started)
    report = None
    for _ in range(120):
        report = p.read(f"{AREA}/reach.json")
        if report and report.endswith("\n"):
            break
        time.sleep(0.5)
    # While the child holds its (no-op) locks: the parent takes the same file's lock at once.
    flock = brief(
        p.run(
            "parent-flock", None, kind="shell", timeout_seconds=20,
            script="flock -n -x sources/a.md -c 'echo parent-got-exclusive-lock' || echo parent-lock-busy",
        )
    )  # fmt: skip
    burn = brief(
        p.run(
            "parent-burn", None, kind="cell",
            code="import time, resource, os\nt = time.process_time()\nwhile time.process_time() - t < 1.5: pass\n"
                 "(resource.getrlimit(resource.RLIMIT_NOFILE), resource.getrlimit(resource.RLIMIT_CPU), os.getpriority(os.PRIO_PROCESS, 0), sorted(os.sched_getaffinity(0))[:4])",
        )
    )  # fmt: skip
    status, _ = p.call(
        "POST", f"/v1/executions/{started['execution_id']}/cancel", {"context": p.context(AREA)}
    )
    ended = brief(p.wait(started, 120))
    # The harm a signal can do: the child's shell sends SIGTERM to the parent's kernel.
    term = brief(
        p.run(
            "child-sigterm", AREA, kind="shell",
            script=f"kill -TERM {kernel} && echo sent-SIGTERM-to-{kernel} || echo refused",
        )
    )  # fmt: skip
    time.sleep(3)
    after_term = brief(
        p.run("parent-after-sigterm", None, kind="cell", code="import os; os.getpid()")
    )
    value = {
        "parent_kernel_pid": kernel,
        "executor_pid": executor,
        "machine": machine,
        "child_sigterm_parent_kernel": term,
        "parent_cell_after_sigterm": after_term,
        "child_report": json.loads(report) if report else None,
        "parent_flock_while_child_holds": flock,
        "parent_kernel_after": burn,
        "child_job": ended,
        "cancel": status,
    }
    p.save("process", value)
    print(json.dumps(value["child_report"], indent=1))
    print("parent flock:", flock["state"], flock["stdout"])
    print("parent burn:", burn["state"], burn["outputs"], burn["error"])
    print("child SIGTERM:", term["state"], term["stdout"], term["error"])
    print("parent after:", after_term["state"], after_term["outputs"], after_term["error"])


def part_provision(p: Probe) -> None:
    before = brief(
        p.run("child-import-before", AREA, kind="cell", code="import sympy", run=CHILD_RUN)
    )
    status, operation = p.call(
        "POST",
        "/v1/provisioning",
        {
            "operation_id": p.op("provision-sympy"),
            "context": p.context(actor="agent"),
            "purpose": "DA-20 lab: a child needs sympy",
            "conda": ["sympy"],
        },
    )
    assert status in (200, 201, 202), operation
    start = time.monotonic()
    while operation.get("state") != "finished" and time.monotonic() - start < 900:
        if operation.get("state") == "awaiting_permission":
            break
        _, operation = p.call(
            "GET",
            f"/v1/provisioning/{operation['provision_id']}?after={operation['seq']}&wait=10",
        )
    after = brief(
        p.run(
            "child-import-after", AREA, kind="cell", run=CHILD_RUN,
            code="import sympy; (sympy.__version__, str(sympy.factor('x**4-1')))",
        )
    )  # fmt: skip
    shell_import = brief(
        p.run(
            "child-shell-import",
            AREA,
            kind="shell",
            script="python3 -c 'import sympy; print(sympy.__version__)'",
        )
    )
    pip = brief(
        p.run(
            "child-pip", AREA, kind="shell", timeout_seconds=300,
            script="pip install --no-input six 2>&1 | tail -n 15; echo \"pip_exit=${PIPESTATUS[0]}\"",
        )
    )  # fmt: skip
    keep = {
        k: operation.get(k)
        for k in (
            "provision_id",
            "state",
            "outcome",
            "stage",
            "reason",
            "error",
            "revision_before",
            "revision_after",
        )
    }
    keep["files"] = len(operation.get("files") or [])
    value = {
        "before": before,
        "operation": keep,
        "after": after,
        "shell_import": shell_import,
        "pip": pip,
    }
    p.save("provision", value)
    print(
        "before:",
        before["state"],
        (before["error"] or {}).get("message", "")[:200]
        if before["error"]
        else before["stdout"][-300:],
    )
    print("provision:", keep)
    print("after:", after["state"], after["outputs"], after["error"])
    print("shell import:", shell_import["stdout"], shell_import["stderr"][-300:])
    print("pip:", pip["state"], pip["stdout"][-2000:])


def part_parent(p: Probe) -> None:
    """The same shell, network and process probes as a parent run: nothing is held."""
    shell = brief(p.run("parent-shell", None, kind="shell", script=f"ROLE=parent\n{SHELL}"))
    files = {name: p.read(name) for name in SEEDED}
    job = brief(
        p.run(
            "parent-job", None, kind="job", script="echo parent-job > sources/job.md && cat sources/job.md && pwd",
            purpose="DA-20 lab: the parent's job", expected_progress="none",
        )
    )  # fmt: skip
    restored = {name: p.write(name, text)[0] for name, text in SEEDED.items()}
    p.save("parent", {"shell": shell, "files_after": files, "job": job, "restored": restored})
    print("parent shell:", shell["state"])
    print(shell["stdout"])
    print("files after:", files)
    print("parent job:", job["state"], job["stdout"])


PARTS = {
    "seed": part_seed,
    "hello": part_hello,
    "shell": part_shell,
    "cell": part_cell,
    "job": part_job,
    "network": part_network,
    "process": part_process,
    "provision": part_provision,
    "parent": part_parent,
}


def main() -> None:
    root = Path(sys.argv[1])
    parts = sys.argv[2:] or list(PARTS)
    probe = Probe(root)
    for name in parts:
        print(f"== {name}", flush=True)
        try:
            PARTS[name](probe)
        except Exception as error:  # keep going: the report reads each part
            probe.save(f"{name}-error", {"error": type(error).__name__, "text": str(error)[:2000]})
            print(f"{name} ERROR {type(error).__name__}: {str(error)[:1000]}", flush=True)


if __name__ == "__main__":
    main()
