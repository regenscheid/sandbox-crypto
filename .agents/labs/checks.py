"""Lab 17: D79's final sync, package turns with the real managers, and revisions, under OpenShell.

Usage: checks.py TOKEN_FILE POLICY_DIGEST PART...   (parts: envs sync packages revisions)
"""

import asyncio
import json
import sys
import time

import aiohttp

TOKEN = open(sys.argv[1]).read().strip()
DIGEST = sys.argv[2]
PARTS = sys.argv[3:]
URL = "http://127.0.0.1:8100"
H = {"Authorization": f"Bearer {TOKEN}"}
STAMP = str(int(time.time()))
PROJECT = "lab17"
A, C = "lab17-a", "lab17-c"

# A PEP 517 backend that takes SLOW_SECONDS to build a wheel of version SLOW_VERSION,
# so that a real `pip install` holds its turn for as long as a check needs.
SLOWPKG = r"""
mkdir -p slowpkg && cat > slowpkg/pyproject.toml <<'EOF'
[build-system]
requires = []
build-backend = "slow_backend"
backend-path = ["."]
EOF
cat > slowpkg/slow_backend.py <<'EOF'
import base64, hashlib, os, time, zipfile

def get_requires_for_build_wheel(config_settings=None):
    return []

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    time.sleep(float(os.environ.get("SLOW_SECONDS", "0")))
    version = os.environ.get("SLOW_VERSION", "0.1")
    info = f"slowpkg-{version}.dist-info"
    files = {
        "slowpkg/__init__.py": b"",
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: slowpkg\nVersion: {version}\n".encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: lab17\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    name = f"slowpkg-{version}-py3-none-any.whl"
    record = []
    with zipfile.ZipFile(os.path.join(wheel_directory, name), "w") as archive:
        for path, data in files.items():
            archive.writestr(path, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            record.append(f"{path},sha256={digest},{len(data)}")
        record.append(f"{info}/RECORD,,")
        archive.writestr(f"{info}/RECORD", "\n".join(record) + "\n")
    return name
EOF
cat > procs.py <<'EOF'
import os, re, sys
pattern = re.compile(sys.argv[1])
for pid in sorted((p for p in os.listdir("/proc") if p.isdigit()), key=int):
    try:
        args = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace").strip()
        ppid = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[1]
    except OSError:
        continue
    if pattern.search(args) and int(pid) != os.getpid():
        print(f"  {pid} (parent {ppid}): {args[:150]}")
EOF
echo ready
"""
# Patterns that do not match their own text, so the script's shell never matches them.
TURN_PROCS = r"python3 procs.py 'provisioning[.]py wrap|pip[ ]install|_in_[p]rocess|slow_[b]ackend'"
LOCK_STATE = r"""python3 - <<'EOF'
import fcntl, os
fd = os.open(os.environ["PROJECT_SHARED"] + "/../.executor/provisioning/lock", os.O_RDONLY)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("  the lock is free")
except OSError as exc:
    print("  the lock is held:", exc)
EOF"""


def ctx(env, session):
    return {"project_id": PROJECT, "session_id": session, "environment_id": env}


async def submit(http, op, env, session, script=None, *, code=None, timeout=None):
    body = {"operation_id": f"{op}-{STAMP}", "context": ctx(env, session)}
    if code is None:
        body.update(kind="shell", script=script)
    else:
        body.update(kind="cell", code=code)
    if timeout is not None:
        body["timeout_seconds"] = timeout
    async with http.post(f"{URL}/v1/executions", headers=H, json=body) as r:
        data = await r.json()
        if r.status not in (200, 201):
            raise RuntimeError(f"{op}: {r.status} {data}")
        return data


async def finish(http, execution_id):
    async with http.get(f"{URL}/v1/executions/{execution_id}/events", headers=H) as r:
        async for line in r.content:
            if line.strip():
                event = json.loads(line)
                if event["type"] == "result":
                    return event["execution"]
    raise RuntimeError(f"no result for {execution_id}")


async def run(http, op, env, session, script=None, **kwargs):
    started = time.monotonic()
    done = await finish(http, (await submit(http, op, env, session, script, **kwargs))["execution_id"])
    return done, time.monotonic() - started


def stream(done, name):
    result = done.get("result") or {}
    return ((result.get(name) or {}).get("text") or "").rstrip("\n")


def show(label, done, took=None, *, output=True):
    result = done.get("result") or {}
    timing = f" in {took:.2f} s" if took is not None else ""
    revisions = f"revision {(done.get('location') or {}).get('environment_revision')} -> {result.get('environment_revision')}"
    print(f"{label}: {done['state']}{timing}; {revisions}; overlapping {result.get('overlapping_change')}")
    if output:
        for name in ("stdout", "stderr"):
            text = stream(done, name)
            if text:
                print(f"  {name}: " + text.replace("\n", "\n    "))
    for notice in result.get("notices") or ():
        print(f"  notice: {notice}")
    if result.get("error"):
        print(f"  error: {result['error']}")


async def envs(http):
    for env in (A, C):
        body = {"environment_id": env, "project_id": PROJECT, "profile": "minimal", "policy": DIGEST}
        started = time.monotonic()
        async with http.post(f"{URL}/v1/environments", headers=H, json=body) as r:
            data = await r.json()
            print(f"environment {env}: {r.status} {data.get('state')} sandbox {data.get('sandbox')} "
                  f"in {time.monotonic() - started:.1f} s")


async def sync(http):
    print("== D79 in the VMs")
    done, _ = await run(http, "fs", A, "a", r"""stat -f -c 'file system: %T' .
python3 - <<'EOF'
import ctypes, errno, os
libc = ctypes.CDLL(None, use_errno=True)
for name, text in (("swap-x", "x"), ("swap-y", "y")):
    open(name, "w").write(text)
code = libc.renameat2(-100, b"swap-x", -100, b"swap-y", 2)
error = errno.errorcode.get(ctypes.get_errno(), "") if code else ""
print("renameat2(RENAME_EXCHANGE):", code, error, "x now holds", open("swap-x").read())
EOF
rm -f swap-x swap-y""")
    show("the swap where the executor writes", done)
    done, took = await run(http, "s1", A, "a", 'echo v1 > "$PROJECT_SHARED/notes.md"; stat -c "inode %i" "$PROJECT_SHARED/notes.md"')
    show("A writes notes.md", done, took)
    done, took = await run(http, "s2", C, "c", 'cat "$PROJECT_SHARED/notes.md"; stat -c "inode %i" "$PROJECT_SHARED/notes.md"')
    show("C reads it", done, took)
    done, took = await run(http, "s3", A, "a", 'echo v2 > "$PROJECT_SHARED/notes.md"')
    show("A writes v2", done, took)
    done, took = await run(http, "s4", C, "c", 'cat "$PROJECT_SHARED/notes.md"; stat -c "inode %i" "$PROJECT_SHARED/notes.md"')
    show("C reads v2 (a new inode: replaced, not rewritten)", done, took)

    print("== a writer in C's kernel keeps the file from being replaced")
    opened = 'import os\nf = open(os.environ["PROJECT_SHARED"] + "/log.txt", "a")\nf.write("c1\\n"); f.flush()\nprint("kernel", os.getpid(), "holds log.txt open for writing")'
    done, took = await run(http, "k1", C, "c", code=opened)
    show("C's kernel opens shared/log.txt to append, writes c1", done, took)
    done, took = await run(http, "k2", A, "a", 'cat "$PROJECT_SHARED/log.txt"; echo a-version > "$PROJECT_SHARED/log.txt"')
    show("A reads it, then replaces it", done, took)
    done, took = await run(http, "k3", C, "c", 'cat "$PROJECT_SHARED/log.txt"')
    show("C's next call (the kernel still writing)", done, took)
    done, took = await run(http, "k4", C, "c", code='f.write("c2\\n"); f.close(); print("closed")')
    show("C's kernel writes c2 and closes it", done, took)
    done, took = await run(http, "k5", C, "c", 'cd "$PROJECT_SHARED" && ls && for f in log*; do echo "$f: $(tr "\\n" " " < "$f")"; done')
    show("C afterwards", done, took)
    done, took = await run(http, "k6", A, "a", 'cd "$PROJECT_SHARED" && ls')
    show("A afterwards", done, took)

    print("== a reader does not keep it")
    done, took = await run(http, "r1", C, "c", code='g = open(os.environ["PROJECT_SHARED"] + "/notes.md"); print(g.read().strip())')
    show("C's kernel opens notes.md to read", done, took)
    done, took = await run(http, "r2", A, "a", 'echo v3 > "$PROJECT_SHARED/notes.md"')
    show("A writes v3", done, took)
    done, took = await run(http, "r3", C, "c", 'cat "$PROJECT_SHARED/notes.md"')
    show("C's next call", done, took)
    done, _ = await run(http, "r4", C, "c", code='g.close()')

    times = []
    for i in range(5):
        _, took = await run(http, f"t{i}", A, "a", "true")
        times.append(took)
    print("a call that changes nothing:", ", ".join(f"{t:.2f}" for t in times), "s")


async def mmap(http):
    print("== a writable shared map in C's kernel keeps the file too")
    done, took = await run(http, "m0", A, "a", 'head -c 4096 /dev/zero > "$PROJECT_SHARED/data.bin"')
    show("A writes data.bin (4 KiB of zeros)", done, took)
    mapped = ('import mmap, os\nfd = os.open(os.environ["PROJECT_SHARED"] + "/data.bin", os.O_RDWR)\n'
              'mm = mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)\nos.close(fd)\n'
              'print("mapped, descriptor closed")')
    done, took = await run(http, "m1", C, "c", code=mapped)
    show("C's kernel maps data.bin shared and writable, then closes its descriptor", done, took)
    done, took = await run(http, "m2", A, "a", 'printf "from A" > "$PROJECT_SHARED/data.bin"')
    show("A replaces data.bin", done, took)
    done, took = await run(http, "m3", C, "c", 'wc -c < "$PROJECT_SHARED/data.bin"')
    show("C's next call (the map still open)", done, took)
    done, took = await run(http, "m4", C, "c", code='mm.close(); print("unmapped")')
    show("C's kernel unmaps it", done, took)
    done, took = await run(http, "m5", C, "c", 'cat "$PROJECT_SHARED/data.bin"; echo')
    show("C's next call", done, took)


async def cancel_turn(http):
    print("== stopping a turn")
    cancelled = await submit(http, "p9", A, "a", "SLOW_SECONDS=60 SLOW_VERSION=0.2 pip install ./slowpkg")
    await asyncio.sleep(6)
    async with http.post(f"{URL}/v1/executions/{cancelled['execution_id']}/cancel", headers=H) as r:
        print("cancel:", r.status)
    show("a: pip install (60 s build), cancelled 6 s in", await finish(http, cancelled["execution_id"]))
    done, took = await run(http, "p10", A, "a", "echo 'processes left:'; " + TURN_PROCS + "\n" + LOCK_STATE
                           + "\nSLOW_VERSION=0.3 pip install ./slowpkg 2>&1 | tail -1")
    show("a: next call, and an install", done, took)



async def idioms(http):
    print("== what scripts find beside the wrappers")
    done, took = await run(http, "i1", A, "a", r"""for c in python pip micromamba jupyter; do printf '%-10s %s\n' "$c" "$(command -v $c)"; done
echo "readlink -f python: $(readlink -f "$(command -v python)")"
echo "first line of python: $(head -n 1 "$(command -v python)" | head -c 60 | tr -d '\0')"
echo "jupyter beside python: $(ls "$(dirname "$(command -v python)")/jupyter" 2>&1)"
echo "python beside pip: $("$(dirname "$(command -v pip)")/python" -c 'import sys; print(sys.executable, sys.prefix)' 2>&1)"
echo "prefix by dirname-dirname: $(dirname "$(dirname "$(command -v python)")")"
echo "CONDA_PREFIX=$CONDA_PREFIX CONDA_EXE=$CONDA_EXE MAMBA_EXE=$MAMBA_EXE"
. "$(dirname "$(command -v micromamba)")/../etc/profile.d/micromamba.sh" 2>/dev/null && echo "micromamba.sh sourced" || echo "no micromamba.sh beside micromamba"
""")
    show("a: command -v and friends", done, took)


async def orphans(http):
    print("== an install started from a kernel, its wrapper killed (kernels never clean up leftovers)")
    listing = ('import os, re\n'
               'def procs():\n'
               '    out = []\n'
               '    for pid in sorted((p for p in os.listdir("/proc") if p.isdigit()), key=int):\n'
               '        try:\n'
               '            args = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\\0", b" ").decode(errors="replace")\n'
               '        except OSError:\n'
               '            continue\n'
               '        if re.search("provisioning[.]py wrap|pip install|_in_process", args):\n'
               '            out.append(f"{pid}: {args[:110]}")\n'
               '    return out\n')
    start = (listing +
             'import signal, subprocess, time\n'
             'env = dict(os.environ, SLOW_SECONDS="40", SLOW_VERSION="0.5")\n'
             'proc = subprocess.Popen(["pip", "install", "./slowpkg"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
             'time.sleep(5)\n'
             'print("before:", *procs(), sep="\\n  ")\n'
             'os.kill(proc.pid, signal.SIGKILL); proc.wait(); time.sleep(1)\n'
             'print(f"SIGKILL to the wrapper {proc.pid}; after:", *procs(), sep="\\n  ")\n')
    done, took = await run(http, "o1", A, "a", code=start, timeout=120)
    show("a's kernel: Popen pip install (40 s build), then SIGKILL to its wrapper", done, took)
    done, took = await run(http, "o2", A, "a", code='print("next cell:", *procs(), sep="\\n  ")')
    show("a's kernel: the next cell", done, took)


async def packages(http):
    print("== package turns with the real pip and micromamba, through the cache")
    done, _ = await run(http, "p0", A, "a", SLOWPKG + "command -v pip micromamba; " + TURN_PROCS)
    show("set up slowpkg in session a", done)

    slow = await submit(http, "p1", A, "a", "SLOW_SECONDS=15 SLOW_VERSION=0.1 pip install ./slowpkg 2>&1 | tail -1")
    await asyncio.sleep(5)
    queued = await submit(http, "p2", A, "b", "echo b ran", timeout=120)
    short = await submit(http, "p3", A, "b", "echo never", timeout=3)
    other, other_took = await run(http, "p4", C, "c", "echo c ran in its own environment")
    slow_done = await finish(http, slow["execution_id"])
    queued_done = await finish(http, queued["execution_id"])
    short_done = await finish(http, short["execution_id"])
    show("a: pip install ./slowpkg (15 s build)", slow_done)
    show("b, submitted 5 s in", queued_done)
    print(f"  b submitted {queued_done['submitted_at']}, started {queued_done['started_at']}; a ended {slow_done['ended_at']}")
    show("b with a 3 s limit, submitted 5 s in", short_done)
    show("c, in the other environment, meanwhile", other, other_took)

    done, took = await run(http, "p5", A, "a", "micromamba install -y toolz 2>&1 | tail -3")
    show("a: micromamba install -y toolz", done, took)
    done, took = await run(http, "p6", A, "a", code="%pip install tomli")
    show("a's kernel: %pip install tomli", done, took, output=False)
    print("  stdout tail:", stream(done, "stdout")[-200:].replace("\n", " | "))
    done, took = await run(http, "p7", A, "a", "/opt/sandbox-science/bin/python -I -m pip install iniconfig 2>&1 | tail -1")
    show("a: python -I -m pip install iniconfig (bypasses the hook)", done, took)
    done, took = await run(http, "p8", A, "a", "pip install attrs 2>&1 | tail -1; pip list 2>/dev/null | grep -ciE '^(attrs|tomli|iniconfig|slowpkg|toolz) '")
    show("a: pip install attrs", done, took)

    await cancel_turn(http)

    script = ("SLOW_SECONDS=30 SLOW_VERSION=0.4 pip install ./slowpkg > pip.log 2>&1 &\nsleep 5\necho before:; " + TURN_PROCS
              + "\nset -- $(python3 procs.py 'provisioning[.]py wrap' | head -1); w=$1; echo \"SIGKILL to the wrapper $w\"; kill -9 \"$w\"; sleep 1\n"
              + "echo after:; " + TURN_PROCS + "\n" + LOCK_STATE + "\nwait; echo \"pip's exit status as the shell saw it: $?\"")
    done, took = await run(http, "p11", A, "a", script)
    show("a: SIGKILL to an install's wrapper", done, took)
    done, took = await run(http, "p12", A, "a", "sleep 1; echo 'a call later:'; " + TURN_PROCS + "; " + LOCK_STATE)
    show("a: the next call", done, took)


async def revisions(http):
    print("== revisions and lockfiles")
    async with http.get(f"{URL}/v1/environments/{A}/revisions", headers=H, params={"limit": "100"}) as r:
        history = await r.json()
        print("revisions:", r.status, "next_after", history.get("next_after"))
    for revision in history.get("revisions", ()):
        lockfile = revision.get("lockfile") or {}
        commands = ["/".join([c["via"], " ".join(c["argv"]), f"exit {c.get('exit_code')}"]) for c in revision.get("commands", ())]
        print(f"  {revision['revision']}: managed {revision.get('managed')}, by {revision.get('changed_by')}, "
              f"lockfile {lockfile.get('conda_packages')} conda / {lockfile.get('pip_distributions')} pip; {commands}")
    numbers = [revision["revision"] for revision in history.get("revisions", ()) if revision.get("lockfile")]
    for number in numbers[-1:]:
        async with http.get(f"{URL}/v1/environments/{A}/revisions/{number}/lockfile", headers=H) as r:
            text = await r.text()
            print(f"lockfile of revision {number}: {r.status}, {len(text)} bytes, X-Content-SHA256 {r.headers.get('X-Content-SHA256', '')[:16]}…")
            print("  names the cache's host:", "host.openshell.internal" in text or ":8090" in text)
            lockfile = json.loads(text)
            for entry in lockfile.get("conda", ()):
                if entry["name"] in ("toolz", "python", "pip"):
                    print("  conda:", {k: entry.get(k) for k in ("name", "version", "channel", "subdir", "file")})
            for entry in lockfile.get("pip", ()):
                print("  pip:", {k: v for k, v in entry.items() if v is not None})


async def main():
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1800)) as http:
        for part in PARTS:
            await {"envs": envs, "sync": sync, "mmap": mmap, "cancel": cancel_turn, "idioms": idioms, "orphans": orphans, "packages": packages, "revisions": revisions}[part](http)


asyncio.run(main())
