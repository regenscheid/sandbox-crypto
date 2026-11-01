# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Qualify B11 (the research image's base environment outside the workspace) live.

A sibling of recovery_acceptance.py that reuses its disposable root, gateway, synthetic
model peer and teardown. From the checkout that stays fixed throughout the lab:

    .venv/bin/python -I .agents/labs/recovery_acceptance.py prepare --duration 600
    .venv/bin/python -I .agents/labs/recovery_acceptance.py up "$LAB"
    .venv/bin/python -I .agents/labs/b11_acceptance.py b11 "$LAB"
    .venv/bin/python -I .agents/labs/b11_acceptance.py cleanup "$LAB"   # or abort "$LAB"

``b11`` creates one new crypto-base environment through the worker, as the orchestrator
does, and drives the worker's real execution and provisioning routes in it: the seed's
time and size, Python and Sage kernels over the read-only base, runtime installs into the
layer (pip, Conda, a replaced base package with a command, a refused removal), the
lockfile against what the environment reports, a kernel and a worker restart, and a
write into the base. Each check is recorded pass or fail in ``evidence/b11-*.json``;
``cleanup`` needs ``evidence/b11-result.json`` (no Sage job runs in this lab).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import signal
import time
import urllib.error
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "recovery_acceptance", Path(__file__).with_name("recovery_acceptance.py")
)
assert _spec is not None
assert _spec.loader is not None
recovery = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recovery)

REPO = recovery.REPO
PROJECT = recovery.PROJECT
SESSION = "b11"
ENVIRONMENT = "b11-env"
BASE = "/opt/sandbox-science-base"
LAYER = "/sandbox/.sandbox-science-prefix"
TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "lost"}
PIP_PACKAGE = "tomli-w"
CONDA_PACKAGE = "tabulate"
UPGRADE = "pygments<2.19"
REMOVAL = "sympy<1.14"

SEED_SCRIPT = r"""set -e
cd /sandbox
printf 'total %s\n' "$(find /sandbox -mindepth 1 | wc -l)"
printf 'total_bytes %s\n' "$(du -s --apparent-size --block-size=1 /sandbox | cut -f1)"
printf 'total_disk_bytes %s\n' "$(du -s --block-size=1 /sandbox | cut -f1)"
printf 'layer %s\n' "$(find /sandbox/.sandbox-science-prefix -mindepth 1 | wc -l)"
printf 'layer_files %s\n' "$(find /sandbox/.sandbox-science-prefix -mindepth 1 -type f | wc -l)"
printf 'layer_links %s\n' "$(find /sandbox/.sandbox-science-prefix -mindepth 1 -type l | wc -l)"
printf 'layer_dirs %s\n' "$(find /sandbox/.sandbox-science-prefix -mindepth 1 -type d | wc -l)"
printf 'layer_bytes %s\n' "$(du -s --apparent-size --block-size=1 /sandbox/.sandbox-science-prefix | cut -f1)"
printf 'layer_file_bytes %s\n' "$(find /sandbox/.sandbox-science-prefix -type f -printf '%s\n' | awk '{s+=$1} END {print s+0}')"
for entry in /sandbox/* /sandbox/.[!.]*; do
  [ -e "$entry" ] || [ -L "$entry" ] || continue
  printf 'top %s %s\n' "${entry#/sandbox/}" "$(find "$entry" | wc -l)"
done
"""

PYTHON_CELL = r"""import json, os, sys
import numpy, sympy
print(json.dumps({
    "prefix": sys.prefix, "base_prefix": sys.base_prefix,
    "prefix_real": os.path.realpath(sys.prefix), "executable": sys.executable,
    "numpy": numpy.__version__, "numpy_file": numpy.__file__,
    "sympy": sympy.__version__, "sympy_file": sympy.__file__,
    "conda_prefix": os.environ.get("CONDA_PREFIX"),
    "path_head": os.environ.get("PATH", "").split(":")[:2],
    "jupyter_path": os.environ.get("JUPYTER_PATH"),
    "site_packages": [p for p in sys.path if p.endswith("site-packages")],
}))
"""

SAGE_CELL = r"""import json, os, sys
import sage.all
from sage.version import version as _sage_version
_curve = EllipticCurve("11a1")
_pari = pari('ellinit("11a1")')
print(json.dumps({
    "prefix": sys.prefix, "base_prefix": sys.base_prefix,
    "prefix_real": os.path.realpath(sys.prefix),
    "sage": _sage_version, "sage_file": sage.all.__file__,
    "conductor": int(_curve.conductor()), "ainvs": [int(a) for a in _curve.ainvs()],
    "pari_ainvs": str(_pari[:5]), "pari_conductor": str(pari.ellglobalred(_pari)[0]),
}))
"""

GP_SCRIPT = r"""printf 'e=ellinit("11a1");print(e[1..5]);print(ellglobalred(e)[1]);\n' | gp -q -f
printf 'gp_path %s\n' "$(command -v gp)"
"""

INSTALLED_CELL = r"""import json, os, subprocess, sys
import tabulate, tomli_w
probe = subprocess.run(
    [sys.executable, "-c", "import json,pygments; print(json.dumps([pygments.__version__, pygments.__file__]))"],
    capture_output=True, text=True, check=True,
)
print(json.dumps({
    "tomli_w_file": tomli_w.__file__, "tomli_w_layer": os.path.realpath(tomli_w.__file__).startswith("/sandbox/.sandbox-science-prefix/"),
    "tabulate": tabulate.__version__, "tabulate_file": tabulate.__file__,
    "tabulate_layer": os.path.realpath(tabulate.__file__).startswith("/sandbox/.sandbox-science-prefix/"),
    "tabulate_works": tabulate.tabulate([[1, 2]], tablefmt="plain"),
    "tomli_w_works": tomli_w.dumps({"b11": 1}),
    "pygments_fresh": json.loads(probe.stdout),
}))
"""

PYGMENTIZE_SCRIPT = r"""set -e
# The first pygmentize on PATH is the executor's package-wrapper link to the layer's;
# the layer's own script must be a file, never a link into the base.
printf 'which %s\n' "$(command -v pygmentize)"
layer=/opt/sandbox-science/bin/pygmentize
if [ -L "$layer" ]; then printf 'kind link %s\n' "$(readlink "$layer")"; else printf 'kind file\n'; fi
printf 'version %s\n' "$(pygmentize -V)"
printf 'highlight %s\n' "$(printf 'x = 1\n' | pygmentize -l python -f html | head -c 60)"
"""

INVENTORY_CELL = r"""import importlib.metadata as md, json, os, re, sys
def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()
conda, owned, names = [], set(), set()
for directory in ("/opt/sandbox-science/conda-meta", "/opt/sandbox-science-base/conda-meta"):
    base = directory.endswith("-base/conda-meta")
    for entry in sorted(os.listdir(directory)):
        if not entry.endswith(".json"):
            continue
        with open(os.path.join(directory, entry)) as handle:
            record = json.load(handle)
        for path in record.get("files", ()):
            match = re.search(r"site-packages/([^/]+)\.(?:dist|egg)-info/", path)
            if match:
                owned.add(match.group(1))
        if base and record["name"] in names:
            continue
        if not base:
            names.add(record["name"])
        conda.append([record["name"], record["version"], record["build"]])
pip, seen = [], set()
for dist in md.distributions():
    name = dist.metadata["Name"]
    key = norm(name)
    if key in seen:
        continue
    seen.add(key)
    installer = (dist.read_text("INSTALLER") or "").strip().lower()
    folder = getattr(dist, "_path", None)
    if installer == "conda" or (folder is not None and folder.name.rsplit(".", 1)[0] in owned):
        continue
    pip.append([key, dist.version, installer or None])
versions = {name: md.version(name) for name in ("pygments", "tabulate", "tomli_w", "sympy", "numpy")}
print(json.dumps({"conda": sorted(conda), "pip": sorted(pip), "versions": versions}))
"""

PIDS_CELL = r"""import json, os
def stat(pid):
    fields = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    name = open(f"/proc/{pid}/comm").read().strip()
    return {"pid": pid, "ppid": int(fields[1]), "start": fields[19], "comm": name}
chain, pid = [], os.getpid()
while pid > 1 and len(chain) < 6:
    item = stat(pid); chain.append(item); pid = item["ppid"]
print(json.dumps(chain))
"""

READ_ONLY_CELL = r"""import json, os
results = {}
for target, mode in (
    ("/opt/sandbox-science-base/b11-probe", "w"),
    ("/opt/sandbox-science-base/lib/python3.14/site-packages/b11-probe.pth", "w"),
    ("/opt/sandbox-science-base/bin/pygmentize", "a"),
):
    try:
        with open(target, mode) as handle:
            handle.write("# b11\n")
        results[target] = {"written": True}
    except OSError as error:
        results[target] = {"type": type(error).__name__, "errno": error.errno,
                           "permission_error": isinstance(error, PermissionError)}
try:
    os.mkdir("/opt/sandbox-science-base/b11-dir")
    results["mkdir"] = {"written": True}
except OSError as error:
    results["mkdir"] = {"type": type(error).__name__, "errno": error.errno,
                        "permission_error": isinstance(error, PermissionError)}
print(json.dumps(results))
"""


class B11Lab(recovery.Lab):
    """The recovery fixture's lab, driven through the worker for B11's checks."""

    def __init__(self, path: Path):
        super().__init__(path)
        self.context = {
            "project_id": PROJECT,
            "session_id": SESSION,
            "environment_id": ENVIRONMENT,
            "actor": "agent",
        }
        self.checks: dict[str, dict] = {}
        self.counter = 0

    # --- worker helpers -------------------------------------------------------------

    def poll(self, check, *, seconds, every=1.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            value = check()
            if value:
                return value
            time.sleep(every)
        raise TimeoutError("deadline waiting for " + getattr(check, "__name__", "check"))

    def operation(self, name):
        self.counter += 1
        return f"b11-{self.counter:02d}-{name}"

    def execute(self, name, kind, *, seconds=900, **fields):
        body = {
            "kind": kind,
            "operation_id": self.operation(name),
            "context": self.context,
            "timeout_seconds": seconds,
            **fields,
        }
        started = time.monotonic()
        execution = self.worker("POST", "/v1/executions", body, timeout=120)

        def finished():
            current = self.worker("GET", "/v1/executions/" + execution["execution_id"])
            return current if current["state"] in TERMINAL else None

        done = self.poll(finished, seconds=seconds + 120)
        done["_elapsed"] = round(time.monotonic() - started, 2)
        self.evidence(
            "b11-step-" + body["operation_id"],
            {**self.summary(done), "stdout_tail": self.stdout(done)[-3000:]},
        )
        return done

    def cell(self, name, code, language="python", **fields):
        return self.execute(name, "cell", language=language, code=code, **fields)

    def shell(self, name, script, **fields):
        return self.execute(name, "shell", script=script, **fields)

    @staticmethod
    def stdout(execution):
        result = execution.get("result") or {}
        return ((result.get("stdout") or {}).get("text")) or ""

    @staticmethod
    def summary(execution):
        result = execution.get("result") or {}
        error = result.get("error") or {}
        return {
            "state": execution["state"],
            "exit_code": result.get("exit_code"),
            "status": result.get("status"),
            "error": {key: error.get(key) for key in ("kind", "name", "value")} if error else None,
            "stderr_tail": ((result.get("stderr") or {}).get("text") or "")[-600:],
            "revision_start": (execution.get("location") or {}).get("environment_revision"),
            "revision_end": result.get("environment_revision"),
            "kernel": execution.get("kernel"),
            "elapsed": execution.get("_elapsed"),
        }

    def last_json(self, execution):
        lines = [line for line in self.stdout(execution).splitlines() if line.strip()]
        if execution["state"] != "succeeded" or not lines:
            raise AssertionError(json.dumps(self.summary(execution)))
        return json.loads(lines[-1])

    def provision(self, name, *, seconds=1500, **fields):
        body = {
            "operation_id": self.operation(name),
            "context": self.context,
            "purpose": "B11 live qualification: " + name,
            **fields,
        }
        started = time.monotonic()
        submitted = self.worker("POST", "/v1/provisioning", body, timeout=120)
        identity = submitted["provision_id"]

        def finished():
            current = self.worker("GET", f"/v1/provisioning/{identity}")
            if current["state"] == "finished" or current.get("outcome") == "awaiting_permission":
                return current
            return None

        done = self.poll(finished, seconds=seconds, every=2.0)
        error = done.get("error") or {}
        summary = {
            "provision_id": identity,
            "request": {key: fields[key] for key in fields},
            "state": done["state"],
            "outcome": done.get("outcome"),
            "reason": done.get("reason"),
            "stage": done.get("stage"),
            "error_message": error.get("message"),
            "next_step": done.get("next_step"),
            "notes": done.get("notes"),
            "revision_before": done.get("revision_before"),
            "revision_after": done.get("revision_after"),
            "files": [
                {key: item.get(key) for key in ("ecosystem", "name", "version", "build", "channel")}
                for item in done.get("files", ())
            ],
            "changes": done.get("changes"),
            "elapsed": round(time.monotonic() - started, 2),
        }
        self.evidence("b11-step-" + body["operation_id"], summary)
        return summary

    def current_lockfile(self):
        history = self.worker("GET", f"/v1/environments/{ENVIRONMENT}/revisions")
        while history.get("next_after") is not None:
            history = self.worker(
                "GET", f"/v1/environments/{ENVIRONMENT}/revisions?after={history['next_after']}"
            )
        latest = history["revisions"][-1]
        reference = latest.get("lockfile")
        lockfile = None
        if reference is not None:
            lockfile = self.worker(
                "GET", f"/v1/environments/{ENVIRONMENT}/revisions/{latest['revision']}/lockfile"
            )
        return latest, reference, lockfile

    def record(self, check, passed, evidence):
        self.checks[check] = {"passed": bool(passed), **evidence}
        self.evidence("b11-" + check, self.checks[check])
        print(("PASS " if passed else "FAIL ") + check, flush=True)

    def guarded(self, check, function):
        try:
            function()
        except Exception as error:
            text = str(error).replace(str(self.path), "$LAB")[-1500:]
            self.record(check, False, {"exception": type(error).__name__, "message": text})

    # --- checks ---------------------------------------------------------------------

    def seed(self):
        policy = recovery.ProjectStore(self.path / "instance/control", readonly=True)
        try:
            project = policy.get(PROJECT)
        finally:
            policy.close()
        body = {
            "environment_id": ENVIRONMENT,
            "project_id": PROJECT,
            "profile": "crypto-base",
            "policy": project.policy,
        }
        started = time.monotonic()
        info = self.worker("POST", "/v1/environments", body, timeout=900)
        returned = time.monotonic() - started

        def ready():
            current = self.worker("GET", "/v1/environments/" + ENVIRONMENT)
            return current if current["state"] == "ready" else None

        info = self.poll(ready, seconds=900)
        ready_at = time.monotonic() - started
        self.remember_sandboxes()
        measured = self.cli(
            "sandbox",
            "exec",
            info["sandbox"],
            "--no-tty",
            "--no-login-shell",
            "--",
            "sh",
            "-c",
            SEED_SCRIPT,
            timeout=300,
        ).stdout
        values, top = {}, {}
        for line in measured.splitlines():
            parts = line.split()
            if parts and parts[0] == "top":
                top[parts[1]] = int(parts[2])
            elif len(parts) == 2:
                values[parts[0]] = int(parts[1])
        evidence = {
            "create_request_seconds": round(returned, 2),
            "create_to_ready_seconds": round(ready_at, 2),
            "environment": {
                key: info.get(key)
                for key in ("state", "profile", "image", "created_at", "updated_at")
            },
            "sandbox_entries": values,
            "top_level_entries": top,
            "docs_reference": {
                "layer_entries": 1691,
                "layer_mb": 3.3,
                "brief_entries": 1690,
                "brief_mb": 0.54,
            },
        }
        passed = info["state"] == "ready" and 0 < values.get("layer", 0) < 5000
        self.record("seed", passed, evidence)

    def kernels(self):
        python = self.cell("python-base", PYTHON_CELL)
        sage = self.cell("sage-base", SAGE_CELL, language="sage")
        gp = self.shell("gp-11a1", GP_SCRIPT)
        found_python = self.last_json(python)
        found_sage = self.last_json(sage)
        gp_lines = self.stdout(gp).splitlines()
        layer_prefix = {"/opt/sandbox-science", LAYER}
        checks = {
            "python_prefix_is_layer": found_python["prefix"] in layer_prefix
            and found_python["prefix_real"] == LAYER,
            "python_base_prefix": found_python["base_prefix"] == BASE,
            "numpy_from_base": found_python["numpy_file"].startswith(BASE + "/"),
            "sympy_from_base": found_python["sympy_file"].startswith(BASE + "/"),
            "sage_prefix_is_layer": found_sage["prefix_real"] == LAYER,
            "sage_base_prefix": found_sage["base_prefix"] == BASE,
            "sage_from_base": os.path.normpath(found_sage["sage_file"]).startswith(BASE + "/"),
            "sage_11a1": found_sage["conductor"] == 11
            and found_sage["ainvs"] == [0, -1, 1, -10, -20],
            "pari_11a1": found_sage["pari_ainvs"].replace(" ", "") == "[0,-1,1,-10,-20]",
            "gp_11a1": gp["state"] == "succeeded"
            and len(gp_lines) >= 2
            and gp_lines[0].replace(" ", "") == "[0,-1,1,-10,-20]"
            and gp_lines[1].strip() == "11",
        }
        self.record(
            "kernels",
            all(checks.values()),
            {
                "checks": checks,
                "python": found_python,
                "sage": found_sage,
                "gp_stdout": gp_lines[:4],
                "executions": {
                    "python": self.summary(python),
                    "sage": self.summary(sage),
                    "gp": self.summary(gp),
                },
            },
        )

    def installs(self):
        before, before_ref, _ = self.current_lockfile()
        conda = self.provision("conda-install", conda=[CONDA_PACKAGE])
        pip = self.provision(
            "pip-install",
            pip=[PIP_PACKAGE],
            pip_reason="B11 lab: a small pure-Python wheel the base lacks, installed by pip",
        )
        installed = self.cell("import-installed", INSTALLED_CELL)
        found_installed = self.last_json(installed)
        upgrade = self.provision("upgrade-pygments", conda=[UPGRADE])
        if upgrade["outcome"] != "completed":
            # A named upgrade is the other route the tool offers for a base package.
            upgrade = {
                "install_attempt": upgrade,
                **self.provision("upgrade-pygments-named", conda=[UPGRADE], action="upgrade"),
            }
        after_upgrade = self.cell("import-after-upgrade", INSTALLED_CELL)
        found_upgrade = self.last_json(after_upgrade)
        command = self.shell("pygmentize", PYGMENTIZE_SCRIPT)
        command_lines = dict(
            line.split(" ", 1) for line in self.stdout(command).splitlines() if " " in line
        )
        latest, reference, lockfile = self.current_lockfile()
        inventory = self.last_json(self.cell("inventory", INVENTORY_CELL))
        consistency = self.compare(lockfile, inventory)
        removal = self.provision("refuse-removal", conda=[REMOVAL])
        after_removal, after_removal_ref, _ = self.current_lockfile()
        pygments_version = found_upgrade["pygments_fresh"][0]
        checks = {
            "conda_completed": conda["outcome"] == "completed",
            "pip_completed": pip["outcome"] == "completed",
            "imports_from_layer": found_installed["tabulate_layer"]
            and found_installed["tomli_w_layer"],
            "upgrade_completed": upgrade["outcome"] == "completed",
            "pygments_below_2_19": tuple(int(x) for x in pygments_version.split(".")[:2]) < (2, 19),
            "pygments_import_from_layer": found_upgrade["pygments_fresh"][1].startswith(
                ("/opt/sandbox-science/", LAYER + "/")
            ),
            "pygmentize_command": command["state"] == "succeeded"
            and command_lines.get("kind") == "file"
            and pygments_version in command_lines.get("version", ""),
            "lockfile_matches_inventory": consistency["equal"],
            "removal_refused": removal["outcome"] in {"failed", "denied"}
            and "would remove" in (removal["error_message"] or "")
            and "sympy" in (removal["error_message"] or ""),
            "removal_changed_nothing": after_removal["revision"] == latest["revision"]
            and (after_removal_ref or {}).get("sha256") == (reference or {}).get("sha256"),
        }
        self.lockfile_after_installs = (latest["revision"], (reference or {}).get("sha256"))
        self.record(
            "runtime_installs",
            all(checks.values()),
            {
                "checks": checks,
                "revision_before": before["revision"],
                "lockfile_before": before_ref,
                "conda": conda,
                "pip": pip,
                "installed": found_installed,
                "upgrade": upgrade,
                "after_upgrade": found_upgrade,
                "pygmentize": command_lines,
                "pygmentize_execution": self.summary(command),
                "revision_after": latest["revision"],
                "lockfile_after": reference,
                "consistency": consistency,
                "removal": removal,
                "revision_after_removal": after_removal["revision"],
                "lockfile_after_removal": after_removal_ref,
            },
        )

    @staticmethod
    def compare(lockfile, inventory):
        def norm(name):
            return re.sub(r"[-_.]+", "-", name).lower()

        if lockfile is None:
            return {"equal": False, "reason": "no lockfile for the current revision"}
        locked_conda = {(p["name"], p["version"], p["build"]) for p in lockfile["conda"]}
        found_conda = {tuple(item) for item in inventory["conda"]}
        locked_pip = {(norm(p["name"]), p["version"]) for p in lockfile["pip"]}
        found_pip = {(item[0], item[1]) for item in inventory["pip"]}
        by_name = {p["name"]: p["version"] for p in lockfile["conda"]}
        versions = {
            name: [inventory["versions"].get(name), by_name.get(name)]
            for name in ("pygments", "tabulate", "sympy", "numpy")
        }
        pip_versions = {norm(p["name"]): p["version"] for p in lockfile["pip"]}
        versions["tomli-w"] = [inventory["versions"].get("tomli_w"), pip_versions.get("tomli-w")]
        return {
            "equal": locked_conda == found_conda
            and locked_pip == found_pip
            and all(a == b for a, b in versions.values()),
            "conda_count": [len(locked_conda), len(found_conda)],
            "pip_count": [len(locked_pip), len(found_pip)],
            "conda_only_locked": sorted(map(list, locked_conda - found_conda))[:20],
            "conda_only_found": sorted(map(list, found_conda - locked_conda))[:20],
            "pip_only_locked": sorted(map(list, locked_pip - found_pip))[:20],
            "pip_only_found": sorted(map(list, found_pip - locked_pip))[:20],
            "pip": sorted(map(list, locked_pip)),
            "versions_found_vs_locked": versions,
            "skipped": lockfile.get("skipped"),
        }

    def worker_restart(self):
        """As recovery_acceptance.worker_cycle: stop launcher supervision keeping the
        sandboxes, start the worker alone on its state, SIGKILL it and start it again."""
        run = recovery.run
        run(
            [
                str(REPO / ".venv/bin/sandbox-science"),
                "down",
                "--keep-sandboxes",
                "--config",
                str(self.config),
            ],
            timeout=180,
        )
        assert not (self.path / "instance/launcher.json").exists()
        args = [
            REPO / ".venv/bin/sandbox-science-worker",
            "serve",
            "--settings",
            self.path / "instance/worker/worker.toml",
            "--log-level",
            "info",
        ]

        def answering():
            try:
                return self.worker("GET", "/v1/environments/" + ENVIRONMENT, timeout=10)
            except (OSError, urllib.error.URLError):
                return None

        self.unit("worker-probe", args, cwd=self.path / "instance/worker")
        self.poll(answering, seconds=180)
        unit = self.prefix + "-worker-probe.service"
        pid = int(
            run(["systemctl", "--user", "show", "--property=MainPID", "--value", unit]).stdout
        )
        process = Path(f"/proc/{pid}")
        stamp = (process / "stat").read_text().rsplit(")", 1)[1].split()[19]
        assert str(self.path / "instance/worker/worker.toml").encode() in (
            process / "cmdline"
        ).read_bytes().split(b"\0")
        os.kill(pid, signal.SIGKILL)
        self.poll(lambda: not process.exists(), seconds=60, every=0.2)
        self.poll(
            lambda: run(["systemctl", "--user", "is-active", unit], check=False).returncode != 0,
            seconds=60,
            every=0.5,
        )
        self.unit("worker-probe", args, cwd=self.path / "instance/worker")
        self.poll(answering, seconds=180)
        return {"killed_worker_start": stamp}

    def restart(self):
        revision, sha = self.lockfile_after_installs
        before_pids = self.last_json(self.cell("pids-before", PIDS_CELL))
        kernels_before = self.worker("POST", "/v1/kernels/observe", {"context": self.context})
        restarted = {
            language: self.worker(
                "POST",
                "/v1/kernels/restart",
                {"context": self.context, "language": language},
                timeout=300,
            )
            for language in ("python", "sage")
        }
        after_kernel = self.last_json(self.cell("after-kernel-restart", INSTALLED_CELL))
        worker = self.worker_restart()
        after_pids = self.last_json(self.cell("pids-after", PIDS_CELL))
        after_worker = self.last_json(self.cell("after-worker-restart", INSTALLED_CELL))
        sage_after = self.last_json(self.cell("sage-after-restart", SAGE_CELL, language="sage"))
        latest, reference, lockfile = self.current_lockfile()
        inventory = self.last_json(self.cell("inventory-after-restart", INVENTORY_CELL))
        consistency = self.compare(lockfile, inventory)
        kernel_pid_changed = before_pids[0]["pid"] != after_pids[0]["pid"] or (
            before_pids[0]["start"] != after_pids[0]["start"]
        )
        executor_changed = [(item["pid"], item["start"]) for item in before_pids[1:]] != [
            (item["pid"], item["start"]) for item in after_pids[1:]
        ]

        def kept(found):
            return (
                found["tabulate_layer"]
                and found["tomli_w_layer"]
                and tuple(int(x) for x in found["pygments_fresh"][0].split(".")[:2]) < (2, 19)
            )

        checks = {
            "installs_survive_kernel_restart": kept(after_kernel),
            "installs_survive_worker_restart": kept(after_worker),
            "sage_after_restart": sage_after["conductor"] == 11,
            "lockfile_unchanged": latest["revision"] == revision
            and (reference or {}).get("sha256") == sha,
            "lockfile_matches_inventory": consistency["equal"],
            "kernel_restarted": kernel_pid_changed,
        }
        self.record(
            "restart",
            all(checks.values()),
            {
                "checks": checks,
                "kernels_before": kernels_before,
                "restart_responses": restarted,
                "process_chain_before": before_pids,
                "process_chain_after": after_pids,
                "executor_process_changed": executor_changed,
                "worker": worker,
                "after_kernel_restart": after_kernel,
                "after_worker_restart": after_worker,
                "revision": [revision, latest["revision"]],
                "lockfile_sha256": [sha, (reference or {}).get("sha256")],
                "consistency": consistency,
            },
        )

    def read_only(self):
        execution = self.cell("write-base", READ_ONLY_CELL)
        found = self.last_json(execution)
        passed = all(
            not item.get("written") and item.get("permission_error") for item in found.values()
        )
        self.record(
            "read_only_base", passed, {"attempts": found, "execution": self.summary(execution)}
        )

    def b11(self):
        assert not (self.path / "evidence/b11-result.json").exists(), "inspect the earlier b11 run"
        self.guarded("seed", self.seed)
        if "create_to_ready_seconds" not in self.checks.get("seed", {}):
            self.finish()  # no environment to check
            return
        self.guarded("kernels", self.kernels)
        self.guarded("runtime_installs", self.installs)
        if hasattr(self, "lockfile_after_installs"):
            self.guarded("restart", self.restart)
        else:
            self.record("restart", False, {"skipped": "runtime installs did not finish"})
        self.guarded("read_only_base", self.read_only)
        self.finish()

    def finish(self):
        self.remember_sandboxes()
        summary = {name: value["passed"] for name, value in self.checks.items()}
        self.evidence(
            "b11-result",
            {
                "checks": summary,
                "passed": bool(summary) and all(summary.values()),
                "identity": {key: self.identity[key] for key in ("revision", "images")},
                "at": time.time(),
            },
        )
        print(json.dumps(summary), flush=True)

    def cleanup(self, *, incomplete=False):
        if not incomplete:
            assert (self.path / "evidence/b11-result.json").exists(), "run b11 first, or abort"
        recovery.OwnedRoot.cleanup(self, incomplete=incomplete)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("b11", "cleanup", "abort"))
    parser.add_argument("lab", type=Path)
    args = parser.parse_args()
    getattr(B11Lab(args.lab), args.action)()


if __name__ == "__main__":
    main()
