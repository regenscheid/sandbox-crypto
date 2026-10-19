# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Own a disposable OpenShell recovery lab; no real credentials or paid model calls.

Run with the checked-out virtualenv: prepare --duration 3600, then up LAB,
start LAB, inspect LAB, and cleanup LAB. Only a marked /var/tmp/ss-recovery-*
directory is accepted. Never use the historical shared lab lifecycle scripts.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION
from sandbox_science.launcher import toml
from sandbox_science.launcher.config import load_config
from sandbox_science.orchestrator.projects import ProjectStore

REPO = Path(__file__).resolve().parents[2]
OS_BIN = Path.home() / ".local/opt/openshell-0.1.3/bin"
PURPOSE = "disposable synthetic recovery acceptance"
PROJECT = "recovery"


def run(args, *, env=None, timeout=120, check=True):
    result = subprocess.run(  # noqa: S603 - fixed fixture commands and marked lab paths
        args, env=env, capture_output=True, text=True, timeout=timeout
    )
    if check and result.returncode:
        raise RuntimeError(f"command {args[0]} exited {result.returncode}: {result.stderr[-4000:]}")
    return result


def stop_units(units: list[str], command=run) -> dict:
    """Try every exact owned unit even when a prior stop fails or times out."""
    receipts = {}
    for unit in units:
        try:
            result = command(["systemctl", "--user", "stop", unit], timeout=30, check=False)
            receipts[unit] = {"exit_code": result.returncode}
        except (OSError, TimeoutError, subprocess.TimeoutExpired) as error:
            receipts[unit] = {"error_type": type(error).__name__}
    return receipts


def private(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


def wait(check, *, seconds=120):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = check()
        if result:
            return result
        time.sleep(0.2)
    raise TimeoutError(f"deadline waiting for {check.__name__}")


def image_recipe(profile: str) -> dict[str, str]:
    """Use the image recipes' own revision calculation, not a floating image tag."""
    revision, short = run(
        [
            "bash",
            "-c",
            'source "$1"; recipe_revision "$2"; printf "%s\\n%s\\n" "$revision" "$short"',
            "recipe",
            str(REPO / "images/revision.sh"),
            profile,
        ]
    ).stdout.splitlines()
    if revision.endswith("-dirty"):
        raise ValueError("commit image inputs before qualification")
    tag = f"sandbox-science/{profile}:{short}"
    found = json.loads(run(["docker", "image", "inspect", tag]).stdout)[0]
    if found["Config"]["Labels"]["org.opencontainers.image.revision"] != revision:
        raise ValueError("cached image revision differs: " + tag)
    return {"tag": tag, "revision": revision, "image_id": found["Id"]}


def prepare(duration: int, static: Path) -> Path:
    if not (static / "index.html").is_file():
        raise ValueError("build web/workspace or supply --static with an existing read-only build")
    images = {
        profile: image_recipe(profile) for profile in ("orchestrator", "minimal", "crypto-base")
    }
    lab = Path("/var/tmp") / ("ss-recovery-" + secrets.token_hex(4))  # noqa: S108 - random private root
    lab.mkdir(mode=0o700)
    ports = {}
    sockets = []
    for name in (
        "gateway",
        "middleware",
        "cache",
        "unused",
        "worker",
        "workspace",
        "preview",
        "control",
        "model",
    ):
        peer = socket.socket()
        peer.bind(("127.0.0.1", 0))
        sockets.append(peer)
        ports[name] = peer.getsockname()[1]
    for peer in sockets:
        peer.close()
    revision = run(["git", "-C", str(REPO), "rev-parse", "HEAD"]).stdout.strip()
    identity = {
        "purpose": PURPOSE,
        "source": str(REPO),
        "revision": revision,
        "unit_prefix": lab.name,
        "ports": ports,
        "duration": duration,
        "images": images,
    }
    private(lab / "identity.json", json.dumps(identity, indent=2) + "\n")
    values = {
        "LAB": str(lab),
        "R": str(REPO),
        "LAB_UNIT": lab.name,
        "OS_BIN": str(OS_BIN),
        "XDG_CONFIG_HOME": str(lab / "config"),
        "XDG_STATE_HOME": str(lab / "state"),
        "XDG_DATA_HOME": str(lab / "data"),
        "OPENSHELL_LOCAL_TLS_DIR": str(lab / "state/openshell/tls"),
        "OPENSHELL_TELEMETRY_ENABLED": "false",
    }
    values.update({"PORT_" + name.upper(): str(port) for name, port in ports.items()})
    private(
        lab / "env.sh",
        "\n".join(f"export {name}={shlex.quote(value)}" for name, value in values.items())
        + '\nexport PATH="$OS_BIN:$PATH"\nOS_RUN_ENV=(\n'
        + "\n".join(
            f"--setenv={key}={shlex.quote(values[key])}"
            for key in (
                "XDG_CONFIG_HOME",
                "XDG_STATE_HOME",
                "XDG_DATA_HOME",
                "OPENSHELL_LOCAL_TLS_DIR",
                "OPENSHELL_TELEMETRY_ENABLED",
            )
        )
        + "\n)\n",
    )
    private(lab / "model.key", "test-secret-recovery-" + secrets.token_hex(16) + "\n")
    data = {
        "state_dir": str(lab / "instance"),
        "projects": [
            {
                "id": PROJECT,
                "profile": "crypto-base",
                "sessions": [
                    "sage",
                    "tree",
                    "literal",
                    "automatic",
                    "history",
                    "narrative",
                    "upload",
                ],
            }
        ],
        "model": {
            "url": f"http://127.0.0.1:{ports['model']}/v1",
            "name": "main",
            "delegated": "delegated",
            "api_key_file": str(lab / "model.key"),
            "api": {"main": "responses", "delegated": "responses"},
            "reasoning_effort": {
                "main": "max",
                "delegated": "max",
                "fold": "high",
                "overview": "low",
            },
        },
        "orchestrator": {
            "image": images["orchestrator"]["tag"],
            "revision": images["orchestrator"]["revision"],
            "control_listen": f"127.0.0.1:{ports['control']}",
        },
        "run_service": {"max_turns": 20, "context_ceiling": 160_000},
        "worker": {
            "listen": f"127.0.0.1:{ports['worker']}",
            "pool_id": lab.name,
            "policy": str(REPO / "examples/policy.yaml"),
        },
        "workspace": {
            "listen": f"127.0.0.1:{ports['workspace']}",
            "preview_listen": f"127.0.0.1:{ports['preview']}",
            "static": str(static.resolve()),
        },
        "openshell": {
            "cli": str(OS_BIN / "openshell"),
            "python": "/opt/sandbox-science/bin/python",
            "isolation_tier": 3,
            "package_cache": f"http://host.openshell.internal:{ports['cache']}",
            "env": {
                key: values[key]
                for key in (
                    "XDG_CONFIG_HOME",
                    "XDG_STATE_HOME",
                    "XDG_DATA_HOME",
                    "OPENSHELL_LOCAL_TLS_DIR",
                    "OPENSHELL_TELEMETRY_ENABLED",
                )
            },
            "environments": {
                "policy_store": str(lab / "middleware/state"),
                "targets": str(lab / "targets.yaml"),
                "ceiling": str(lab / "middleware/pool-ceiling.yaml"),
                "profiles": {
                    "minimal": images["minimal"]["tag"],
                    "crypto-base": images["crypto-base"]["tag"],
                },
            },
        },
    }
    private(lab / "sandbox-science.toml", toml.dumps(data))
    load_config(lab / "sandbox-science.toml")
    # A real compute loop: one CPU, one Sage process, bounded output and durable
    # checkpoints. Work per batch is deterministic; duration controls batch count.
    code = f"""import hashlib,json,os,time
from pathlib import Path
from sage.all import GF,matrix
os.sched_setaffinity(0,{{min(os.sched_getaffinity(0))}})
p=Path("sage-starts.jsonl")
with p.open("a") as out: out.write(json.dumps({{"pid":os.getpid(),"at":time.time()}})+"\\n")
field=GF(65537)
a=matrix(field,32,32,lambda i,j: (i+1)*(j+3)+(i==j))
identity=matrix.identity(field,32)
inv=(a+identity).inverse()
assert (a+identity)*inv==identity
start=time.monotonic(); last=start-30; count=0; digest=hashlib.sha256()
while time.monotonic()-start < {duration}:
 b=(a+identity)*inv
 assert b==identity
 count+=1
 digest.update(str(b[0,0]).encode())
 now=time.monotonic()
 if now-last>=30:
  value={{"batches":count,"elapsed":now-start,"sha256":digest.hexdigest(),"pid":os.getpid()}}
  Path("sage-checkpoint.json").write_text(json.dumps(value)+"\\n")
  print(json.dumps(value),flush=True); last=now
value={{"batches":count,"elapsed":time.monotonic()-start,"sha256":digest.hexdigest(),"pid":os.getpid(),"verified":True}}
Path("sage-result.json").write_text(json.dumps(value)+"\\n")
print(json.dumps(value),flush=True)
"""
    private(
        lab / "sage-job.sh",
        "export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1\n"
        + "/opt/sandbox-science/bin/python -I - <<'PY'\n"
        + code
        + "PY\n",
    )
    private(lab / "owned-sandboxes.json", "{}\n")
    return lab


class Lab:
    def __init__(self, path: Path):
        self.path = path.resolve()
        assert self.path.parent == Path("/var/tmp")  # noqa: S108 - marked private fixture root
        assert self.path.name.startswith("ss-recovery-")
        self.identity = json.loads((self.path / "identity.json").read_text())
        assert self.identity["purpose"] == PURPOSE
        assert self.identity["source"] == str(REPO)
        self.ports = self.identity["ports"]
        self.prefix = self.identity["unit_prefix"]
        assert self.prefix == self.path.name
        self.config = self.path / "sandbox-science.toml"
        self.env = {**os.environ, **load_config(self.config).openshell.env}
        self.env.update(LAB=str(self.path), R=str(REPO), LAB_UNIT=self.prefix, OS_BIN=str(OS_BIN))
        self.env.update({"PORT_" + name.upper(): str(port) for name, port in self.ports.items()})
        self.base = f"http://127.0.0.1:{self.ports['workspace']}"

    def unit(self, suffix, args, *, cwd=None):
        return run(
            [
                "systemd-run",
                "--user",
                "--unit=" + self.prefix + "-" + suffix,
                "--collect",
                "-q",
                "--working-directory=" + str(cwd or self.path),
                *map(str, args),
            ],
            env=self.env,
        )

    def cli(self, *args, **kwargs):
        return run([str(OS_BIN / "openshell"), *map(str, args)], env=self.env, **kwargs)

    def read(self, sql, args=()):
        projects = ProjectStore(self.path / "instance/control")
        try:
            with projects.control.read() as tx:
                return tx.sql.execute(sql, args).fetchall()
        finally:
            projects.close()

    def evidence(self, name, value):
        (self.path / "evidence").mkdir(exist_ok=True)
        private(self.path / "evidence" / (name + ".json"), json.dumps(value, indent=2) + "\n")

    def ready(self):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.ports['control']}/health", timeout=2
            ) as response:
                return json.load(response).get("runtime_connected")
        except OSError:
            return False

    def remember_sandboxes(self):
        value = json.loads(self.cli("sandbox", "list", "-o", "json").stdout)
        assert not value.get("next_page_token")
        path = self.path / "owned-sandboxes.json"
        owned = json.loads(path.read_text())
        owned.update({item["id"]: item["name"] for item in value["sandboxes"]})
        private(path, json.dumps(owned, indent=2) + "\n")
        return value["sandboxes"]

    def login(self):
        wait(lambda: (self.path / "instance/workspace-login").is_file())
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self.opener.open(
            (self.path / "instance/workspace-login").read_text().strip(), timeout=10
        ).close()

    def post(self, session, action, body, *, timeout=30):
        request = urllib.request.Request(  # noqa: S310 - fixed owned loopback URL
            self.base + f"/api/v0/projects/{PROJECT}/sessions/{session}/{action}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Origin": self.base},
        )
        with self.opener.open(request, timeout=timeout) as response:
            return json.load(response)

    def worker(self, method, path, body=None, *, operator=False, timeout=60):
        client = "workspace" if operator else "harness"
        token = (self.path / f"instance/tokens/{client}-worker.token").read_text().strip()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.ports['worker']}" + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + token,
                PROTOCOL_HEADER: str(PROTOCOL_VERSION),
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed owned loopback URL
            return json.load(response)

    def up(self):
        run(["bash", str(REPO / ".agents/labs/recovery_acceptance_up.sh")], env=self.env)
        self.cli(
            "provider",
            "profile",
            "import",
            "-f",
            REPO / "examples/openshell/profiles/github-token.yaml",
        )
        env = {**self.env, "GITHUB_TOKEN": "test-secret-recovery-github"}
        run(
            [
                str(OS_BIN / "openshell"),
                "provider",
                "create",
                "--name",
                "github-readonly",
                "--type",
                "github-token",
                "--credential",
                "GITHUB_TOKEN",
            ],
            env=env,
        )
        self.unit(
            "model",
            [
                REPO / ".venv/bin/python",
                "-I",
                REPO / ".agents/labs/recovery_acceptance_model.py",
                self.path,
            ],
        )
        self.unit("launcher", [REPO / ".venv/bin/sandbox-science", "up", "--config", self.config])
        wait(self.ready, seconds=240)
        self.remember_sandboxes()
        run(
            [
                str(REPO / ".venv/bin/sandbox-science-harness"),
                "project",
                "delegation",
                "--state-dir",
                str(self.path / "instance/control"),
                "--project",
                PROJECT,
                "--depth",
                "2",
                "--task-tokens",
                "2000000",
            ]
        )
        self.evidence("ready", {"ready": True, "identity": self.identity})

    def start(self):
        self.login()
        sent = self.post(
            "sage",
            "messages",
            {
                "message_id": "lab-sage-long",
                "text": "SAGE-LONG: start the sustained finite-field job once.",
            },
        )
        self.evidence("sage-admission", sent)

    def inspect(self):
        print(
            json.dumps(
                {
                    "ready": self.ready(),
                    "works": self.read(
                        "SELECT session,work,current,state,reason FROM research_work ORDER BY session,work"
                    ),
                    "leases": self.read("SELECT project_id,session_id,run FROM run_leases"),
                },
                indent=2,
            )
        )

    def intent(self, session, tool):
        from sandbox_science.orchestrator.history.operations import ToolJournal

        found = self.read(
            "SELECT run,operation_id FROM tool_intents WHERE project=? "
            "AND session=? AND tool=? ORDER BY seq DESC LIMIT 1",
            (PROJECT, session, tool),
        )
        if not found:
            return None
        projects = ProjectStore(self.path / "instance/control")
        try:
            value = ToolJournal(projects.control).intent(PROJECT, session, *found[0])
            return {**value, "operation_id": found[0][1]}
        finally:
            projects.close()

    def shell_running(self, session):
        intent = self.intent(session, "shell")
        if intent is None:
            return None
        found = self.worker(
            "POST",
            "/v1/operations/lookup",
            {"context": intent["context"], "operation_id": intent["operation_id"]},
        )
        return intent if (found.get("execution") or {}).get("state") == "running" else None

    def replacement(self, session, original_intent):
        original = json.loads((self.path / "instance/orchestrator/sandbox.json").read_text())
        before = json.loads((self.path / "instance/launcher.json").read_text())
        self.remember_sandboxes()
        self.evidence(
            session + "-before-loss",
            {
                "sandbox": original,
                "intent": original_intent,
                "parts": [
                    {key: item[key] for key in ("name", "pid", "started")}
                    for item in before["parts"]
                ],
            },
        )
        self.cli("sandbox", "delete", original["name"], timeout=120)
        command = [
            str(REPO / ".venv/bin/sandbox-science"),
            "replace-orchestrator",
            "--config",
            str(self.config),
        ]
        started = time.time()
        replaced = run(command, timeout=240, check=False)
        if replaced.returncode:
            refusal = "old runtime control connection did not settle; replacement refused"
            if refusal not in replaced.stderr:
                raise RuntimeError("replacement did not succeed: " + replaced.stderr[-2000:])
            # A known pre-creation refusal is retryable after its precise
            # condition clears. Unknown outcomes never enter this branch.
            self.evidence(
                session + "-drain-refusal",
                {"error": refusal, "started": started, "refused": time.time()},
            )
            wait(lambda: not self.ready(), seconds=120)
            replaced = run(command, timeout=240)
        wait(self.ready)
        self.remember_sandboxes()
        after = json.loads((self.path / "instance/launcher.json").read_text())
        before_ids = {p["name"]: (p["pid"], p["started"]) for p in before["parts"]}
        after_ids = {p["name"]: (p["pid"], p["started"]) for p in after["parts"]}
        assert all(
            before_ids[name] == after_ids[name]
            for name in ("worker", "control-service", "workspace")
        )
        self.login()
        resumed = self.post(session, "resume", {})
        self.evidence(
            session + "-resume",
            {
                "resume": resumed,
                "replacement": json.loads(
                    (self.path / "instance/orchestrator/sandbox.json").read_text()
                ),
                "replacement_command_succeeded": replaced.returncode == 0,
            },
        )

    def settled(self, session):
        rows = self.read(
            "SELECT work,current,state,reason FROM research_work WHERE session=? ORDER BY work",
            (session,),
        )
        return rows if rows and all(row[2] == "completed" for row in rows) else None

    def results(self, session):
        from sandbox_science.orchestrator.history.operations import ToolJournal

        records = self.read(
            "SELECT message_id FROM archive_tool_records WHERE project=? AND session=? ORDER BY rowid",
            (PROJECT, session),
        )
        projects = ProjectStore(self.path / "instance/control")
        try:
            tools = [ToolJournal(projects.control).record(PROJECT, row[0]) for row in records]
        finally:
            projects.close()
        return {
            "works": self.read(
                "SELECT work,current,state,reason FROM research_work WHERE session=? ORDER BY work",
                (session,),
            ),
            "attempts": self.read(
                "SELECT work,run,predecessor FROM research_attempts WHERE session=? ORDER BY run",
                (session,),
            ),
            "intents": self.read(
                "SELECT run,operation_id,tool FROM tool_intents WHERE session=? ORDER BY seq",
                (session,),
            ),
            "tools": tools,
        }

    def tree(self):
        self.login()
        self.post(
            "tree",
            "messages",
            {"message_id": "lab-tree-loss", "text": "TREE-LOSS: qualify recursive recovery."},
        )
        original = wait(lambda: self.shell_running("tree"), seconds=240)
        self.replacement("tree", original)
        wait(lambda: self.settled("tree"), seconds=180)
        result = self.results("tree")
        shells = [item for item in result["tools"] if item.get("tool") == "shell"]
        assert len(result["works"]) == 2, result
        assert len(result["attempts"]) == 4, result
        assert len([item for item in result["intents"] if item[2] == "shell"]) == 1
        assert len(shells) == 1
        assert shells[0]["reconciled"] is True
        assert shells[0]["execution"]["result"]["stdout"]["text"] == "child-holdingchild-done"
        self.evidence("tree-result", result)
        print(
            "PASS recursive agent interruption and original child effect reconciliation", flush=True
        )

    def literal(self):
        self.login()
        self.post(
            "literal",
            "messages",
            {
                "message_id": "lab-literal-loss",
                "text": "LITERAL-LOSS: qualify opt-in literal reconstruction.",
            },
        )
        original = wait(lambda: self.shell_running("literal"), seconds=240)
        context = original["context"]
        before = self.worker("POST", "/v1/kernels/observe", {"context": context})
        assert len(before["kernels"]) == 1, before
        environment = self.worker("GET", "/v1/environments/" + context["environment_id"])
        # The owned VM contains one idle IPython kernel for this session. Verify
        # its PID/start pair immediately before killing that process alone.
        code = """import json,os,pathlib,signal
found=[]
for path in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
 try:
  words=path.read_bytes().split(b'\\0')
  if b'ipykernel_launcher' in words:
   found.append((int(path.parent.name),(path.parent/'stat').read_text().rsplit(')',1)[1].split()[19]))
 except (OSError,ValueError): pass
assert len(found)==1,found
pid,stamp=found[0]
assert pathlib.Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]==stamp
os.kill(pid,signal.SIGKILL)
print(json.dumps({'pid':pid,'start':stamp}))
"""
        killed = json.loads(
            self.cli(
                "sandbox",
                "exec",
                environment["sandbox"],
                "--no-tty",
                "--no-login-shell",
                "--",
                "/opt/sandbox-science/bin/python",
                "-I",
                "-c",
                code,
            ).stdout
        )
        absent = wait(
            lambda: (
                (value if not value["kernels"] else None)
                if (value := self.worker("POST", "/v1/kernels/observe", {"context": context}))
                else None
            )
        )
        self.evidence("literal-kernel-loss", {"before": before, "absent": absent, "killed": killed})
        self.replacement("literal", original)
        wait(lambda: self.settled("literal"), seconds=180)
        result = self.results("literal")
        after = self.worker("POST", "/v1/kernels/observe", {"context": context})
        assert len(after["kernels"]) == 1
        assert after["kernels"][0]["generation"] > before["kernels"][0]["generation"]
        python_results = [item for item in result["tools"] if item.get("tool") == "python"]
        assert len(python_results) == 2, python_results
        assert python_results[-1]["execution"]["result"]["stdout"]["text"] == "42\n"
        result["after"] = after
        result["observations"] = self.read(
            "SELECT kind,identity,detail FROM research_observations WHERE session='literal'"
        )
        restored = [
            json.loads(detail)
            for kind, _, detail in result["observations"]
            if kind == "kernel_restore"
        ]
        assert restored == [{"state": "done", "cells": 1}], restored
        self.evidence("literal-result", result)
        print(
            "PASS literal kernel reconstruction after real kernel and agent process loss",
            flush=True,
        )

    def sage(self):
        records = self.results("sage")["tools"]
        jobs = [item["execution"] for item in records if item.get("tool") == "start_job"]
        assert jobs, "Sage job has not been admitted"
        return self.worker("GET", "/v1/jobs/" + jobs[-1]["execution_id"])

    def automatic(self, session="automatic", marker="AUTO-RECOVERY"):
        self.login()
        self.post(
            session,
            "messages",
            {
                "message_id": "lab-" + session,
                "text": marker + ": recover after the one injected premature stream EOF.",
            },
        )
        try:
            wait(lambda: self.settled(session), seconds=180)
        finally:
            result = self.results(session)
            result["records"] = self.read(
                "SELECT name,json_extract(record,'$.stopped'),json_extract(record,'$.failure') "
                "FROM runs WHERE session_id=? ORDER BY name",
                (session,),
            )
            result["decisions"] = self.read(
                "SELECT identity,detail FROM research_observations WHERE session=? "
                "AND kind='auto_resume'",
                (session,),
            )
            result["resumes"] = self.read(
                "SELECT request,status,reason FROM research_resumes WHERE session=?", (session,)
            )
            self.evidence(session + "-result", result)
        assert len(result["works"]) == 1
        assert len(result["attempts"]) == 2
        assert result["records"][0][1:] == ("error", "model")
        assert len(result["decisions"]) == 1
        assert len(result["resumes"]) == 1
        assert result["resumes"][0][1] == "finished"
        print("PASS durable automatic continuation after saved model-phase failure", flush=True)

    def history_result(self, session):
        from sandbox_science.orchestrator.history import Archive

        result = self.results(session)
        assert len(result["works"]) == 3
        assert all(row[2] == "completed" for row in result["works"])
        searches = [item for item in result["tools"] if item.get("tool") == "history_search"]
        reads = [item for item in result["tools"] if item.get("tool") == "history_read"]
        assert len(searches) == 1
        assert len(reads) == 1
        assert searches[0]["history_search"]["scope"] == "subtree"
        selected = searches[0]["history_search"]["returned"]
        assert len(selected) == 1
        assert reads[0]["history_read"]["read"] == selected
        projects = ProjectStore(self.path / "instance/control")
        try:
            archived = Archive(projects.control).message(PROJECT, selected[0])
            result["retrieved"] = {
                "message_id": archived.message_id,
                "run": archived.run,
                "kind": archived.entry.kind,
                "text": archived.entry.text,
            }
        finally:
            projects.close()
        shell = next(item for item in result["tools"] if item.get("tool") == "shell")
        assert shell["execution"]["result"]["stdout"]["text"] == "descendant-archive-proof-981c"
        assert archived.run == shell["execution"]["context"]["authority"]["run"]
        assert "descendant-archive-proof-981c" in archived.entry.text
        self.evidence("history-result", result)
        return result

    def history(self):
        self.login()
        self.post(
            "history",
            "messages",
            {
                "message_id": "lab-descendant-history",
                "text": "HISTORY-TREE: prove a delegated parent reads its child archive.",
            },
        )
        wait(lambda: self.settled("history"), seconds=180)
        self.history_result("history")
        print("PASS delegated parent retrieves its descendant's full archive result", flush=True)

    def narrative(self):
        """Qualify model-written child folding after configuring the final runtime."""
        from sandbox_science.orchestrator.continuity.plans import spent_tokens

        self.login()
        self.post(
            "narrative",
            "messages",
            {
                "message_id": "lab-child-narrative",
                "text": "NARRATIVE-PARENT-PRIVATE: delegate the synthetic context probe.",
            },
        )
        try:
            wait(lambda: self.settled("narrative"), seconds=240)
        finally:
            result = self.results("narrative")
            result["records"] = [
                json.loads(row[0])
                for row in self.read(
                    "SELECT record FROM runs WHERE session_id='narrative' ORDER BY name"
                )
            ]
            self.evidence("narrative-result", result)
        assert len(result["works"]) == len(result["attempts"]) == 2
        (child,) = [record for record in result["records"] if record["role"] == "delegated"]
        folds = child["summaries"]["folds"]
        assert folds["calls_total"] > 0
        assert any(fold["state"] == "applied" for fold in folds["folds"])
        assert child["model_profiles"]["fold"]["alias"] == "delegated"
        assert child["model_profiles"]["fold"]["reasoning_effort"] == "high"
        assert child["model_profiles"]["overview"]["reasoning_effort"] == "low"
        (work,) = self.read(
            "SELECT work FROM research_work WHERE session='narrative' AND parent IS NOT NULL"
        )
        child_work = work[0]
        (original,) = self.read(
            "SELECT json_extract(snapshot,'$.request.tokens') FROM research_blueprints WHERE session='narrative' AND work=?",
            (child_work,),
        )
        assert original[0] == 2_000_000
        canonical = self.read(
            "SELECT identity,usage FROM research_model_calls WHERE session='narrative' AND run=? AND kind='fold' ORDER BY created",
            (child_work,),
        )
        assert canonical
        assert all(row[1] is not None for row in canonical)
        copies = self.read(
            "SELECT item FROM run_collections WHERE session='narrative' AND run=? AND collection='fold_calls' ORDER BY position",
            (child_work,),
        )
        copied = [json.loads(row[0]) for row in copies]
        assert {item["intent_id"] for item in copied} == {row[0] for row in canonical}
        assert len(copied) == len(canonical)
        direct = self.read(
            "SELECT usage FROM archive_model_usage WHERE session='narrative' AND run=?",
            (child_work,),
        )
        counted = [json.loads(row[1]) for row in canonical] + [json.loads(row[0]) for row in direct]
        expected = sum(item["input_tokens"] + item["output_tokens"] for item in counted)
        projects = ProjectStore(self.path / "instance/control")
        try:
            with projects.control.read() as tx:
                total = spent_tokens(tx.sql, PROJECT, "narrative", child_work)
        finally:
            projects.close()
        assert total == expected < original[0]
        observations = [
            json.loads(line)
            for line in (self.path / "model-observations.jsonl").read_text().splitlines()
        ]
        narrators = [item for item in observations if item.get("narrative_kind")]
        assert narrators
        assert all(
            item["model"] == "delegated" and not item["contains_parent_private"]
            for item in narrators
        )
        assert all(
            item["reasoning"]["effort"] == ("high" if item["narrative_kind"] == "fold" else "low")
            for item in narrators
        )
        result["accounting"] = {
            "work": child_work,
            "original_grant": original[0],
            "spent_tokens": total,
            "canonical_folds": canonical,
            "fold_copies": copied,
            "narrators": [
                {key: value for key, value in item.items() if key != "output"} for item in narrators
            ],
        }
        self.evidence("narrative-result", result)
        print(
            "PASS child narrative alias/effort, canonical accounting and original grant",
            flush=True,
        )

    def worker_cycle(self):
        assert not self.read("SELECT run FROM run_leases")
        before = self.sage()
        assert before["execution"]["state"] == "running"
        assert before["stdout"]["text"]
        self.evidence("worker-before", before)
        # Launcher deliberately does not restart a failed worker. Stop its
        # supervision cleanly, retaining sandbox daemons, then test actual hard
        # death of the same worker state under a separately owned transient unit.
        run(
            [
                str(REPO / ".venv/bin/sandbox-science"),
                "down",
                "--keep-sandboxes",
                "--config",
                str(self.config),
            ],
            timeout=120,
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
        self.unit("worker-probe", args, cwd=self.path / "instance/worker")

        def ready_job():
            try:
                job = self.sage()
                return job if job["execution"]["state"] == "running" else None
            except OSError:
                return None

        restarted = wait(ready_job)
        unit = self.prefix + "-worker-probe.service"
        pid = int(
            run(["systemctl", "--user", "show", "--property=MainPID", "--value", unit]).stdout
        )
        process = Path(f"/proc/{pid}")
        stamp = (process / "stat").read_text().rsplit(")", 1)[1].split()[19]
        command = (process / "cmdline").read_bytes().split(b"\0")
        assert str(self.path / "instance/worker/worker.toml").encode() in command
        assert (process / "stat").read_text().rsplit(")", 1)[1].split()[19] == stamp
        os.kill(pid, signal.SIGKILL)
        wait(lambda: not process.exists())
        wait(lambda: run(["systemctl", "--user", "is-active", unit], check=False).returncode != 0)
        self.unit("worker-probe", args, cwd=self.path / "instance/worker")
        recovered = wait(ready_job)
        execution = before["execution"]
        assert recovered["execution"]["execution_id"] == execution["execution_id"]
        assert recovered["execution"]["attempt_id"] == execution["attempt_id"]
        assert recovered["execution"]["operation_id"] == execution["operation_id"]
        assert recovered["execution"]["started_at"] == execution["started_at"]
        found = self.worker(
            "POST",
            "/v1/operations/lookup",
            {"context": execution["context"], "operation_id": execution["operation_id"]},
        )
        assert found["execution"]["execution_id"] == execution["execution_id"]
        self.evidence(
            "worker-recovered",
            {
                "before": before,
                "after_launcher_stop": restarted,
                "killed": {"pid": pid, "start": stamp},
                "recovered": recovered,
                "lookup": found,
            },
        )
        print("PASS SIGKILL worker recovery with same live Sage job/attempt/operation", flush=True)

    def upload(self):
        """Send an exact 100 MiB original and verify the real worker delivery."""
        self.login()
        size = 100 * 1024 * 1024
        source = self.path / "upload-payload.bin"
        assert not source.exists(), "inspect an existing upload outcome before repeating"
        digest = hashlib.sha256()
        chunk = bytes(range(256)) * 4096
        with source.open("xb") as output:
            for _ in range(100):
                output.write(chunk)
                digest.update(chunk)
        metadata = {
            "upload_id": "upload-100m",
            "filename": "payload.bin",
            "media_type": "application/octet-stream",
        }
        endpoint = self.base + f"/api/v0/projects/{PROJECT}/sessions/upload/uploads"
        with source.open("rb") as data:
            request = urllib.request.Request(  # noqa: S310 - owned loopback upload URL
                endpoint,
                data=data,
                headers={
                    "Origin": self.base,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(size),
                    "X-Upload-Metadata": quote(json.dumps(metadata)),
                },
            )
            with self.opener.open(request, timeout=180) as response:
                original = json.load(response)
        assert original["size"] == size
        assert original["sha256"] == digest.hexdigest()
        downloaded, count = hashlib.sha256(), 0
        with self.opener.open(endpoint + "/upload-100m/content", timeout=180) as response:
            while part := response.read(1024 * 1024):
                downloaded.update(part)
                count += len(part)
        assert count == size
        assert downloaded.hexdigest() == digest.hexdigest()
        self.evidence(
            "upload-original",
            {
                "original": original,
                "downloaded_size": count,
                "downloaded_sha256": downloaded.hexdigest(),
            },
        )
        admission = self.post(
            "upload",
            "messages",
            {
                "message_id": "lab-upload-delivery",
                "text": "The synthetic upload must be delivered before this message is admitted.",
                "attachments": ["upload-100m"],
            },
            timeout=180,
        )
        self.evidence("upload-admitted", admission)
        wait(lambda: self.settled("upload"), seconds=180)
        (environment,) = self.read(
            "SELECT environment_id FROM sessions WHERE project_id=? AND session_id='upload'",
            (PROJECT,),
        )
        found = self.worker(
            "POST",
            "/v1/files/read",
            {
                "context": {
                    "project_id": PROJECT,
                    "session_id": "upload",
                    "environment_id": environment[0],
                    "actor": "user",
                },
                "path": original["path"],
                "offset": 0,
                "length": 0,
            },
            operator=True,
        )
        assert found["size"] == size
        assert found["sha256"] == digest.hexdigest()
        self.evidence(
            "upload-delivered",
            {
                "admission": admission,
                "worker": found,
                "original": original,
                "same_sage_job": self.sage()["execution"]["execution_id"],
            },
        )
        print(
            "PASS exact 100 MiB original, streamed client download and worker copy digest",
            flush=True,
        )

    def file(self, context, path):
        result = self.worker(
            "POST",
            "/v1/files/read",
            {"context": context, "path": path, "offset": 0, "length": 1024 * 1024},
        )
        assert not result.get("truncated", False)
        return base64.b64decode(result["content_b64"]).decode()

    def finish(self):
        job = self.sage()
        assert job["execution"]["state"] == "succeeded", job["execution"]["state"]
        assert job["execution"]["result"]["exit_code"] == 0
        context = job["execution"]["context"]
        starts = [json.loads(line) for line in self.file(context, "sage-starts.jsonl").splitlines()]
        final = json.loads(self.file(context, "sage-result.json"))
        assert len(starts) == 1, starts
        assert final["pid"] == starts[0]["pid"]
        assert final["elapsed"] >= self.identity["duration"]
        assert final["verified"] is True
        expected = hashlib.sha256()
        count = final["batches"]
        while count:
            chunk = min(count, 65536)
            expected.update(b"1" * chunk)
            count -= chunk
        assert final["sha256"] == expected.hexdigest()
        # The terminal read is a receipt for the original operation, not a replay.
        found = self.worker(
            "POST",
            "/v1/operations/lookup",
            {"context": context, "operation_id": job["execution"]["operation_id"]},
        )
        assert found["execution"]["execution_id"] == job["execution"]["execution_id"]
        self.evidence("sage-final", {"job": job, "starts": starts, "final": final, "lookup": found})
        print("PASS sustained Sage computation and digest after worker/agent losses", flush=True)

    def stop_owned_services(self, cleanup_error: Exception):
        """A failed gateway query must not leave the owned host units running.

        This deliberately cannot certify VM deletion or remove the private root:
        that requires the normal gateway/ownership reconciliation to succeed.
        """
        assert json.loads((self.path / "identity.json").read_text()) == self.identity
        units = [
            self.prefix + "-" + name + ".service"
            for name in ("launcher", "worker-probe", "model", "gateway", "middleware", "cache")
        ]
        processes = {}
        inventory_error = None
        try:
            for unit in units:
                group = run(
                    ["systemctl", "--user", "show", "--property=ControlGroup", "--value", unit],
                    check=False,
                ).stdout.strip()
                if not group:
                    continue
                for file in (Path("/sys/fs/cgroup") / group.lstrip("/")).rglob("cgroup.procs"):
                    for value in file.read_text().splitlines():
                        pid = int(value)
                        try:
                            processes[pid] = (
                                Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
                            )
                        except FileNotFoundError:
                            continue
        except Exception as error:
            inventory_error = type(error).__name__
        finally:
            stops = stop_units(units)
        status = {
            unit: run(["systemctl", "--user", "is-active", unit], check=False).stdout.strip()
            for unit in units
        }
        remaining = []
        for pid, started in processes.items():
            try:
                stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            except FileNotFoundError:
                continue
            if stat[19] == started and stat[0] != "Z":
                remaining.append(pid)
        open_ports = []
        for port in self.ports.values():
            with socket.socket() as peer:
                peer.settimeout(0.2)
                if peer.connect_ex(("127.0.0.1", port)) == 0:
                    open_ports.append(port)
        receipt = {
            "cleanup_error_type": type(cleanup_error).__name__,
            "stops": stops,
            "unit_status": status,
            "observed_process_count": len(processes),
            "process_inventory_error": inventory_error,
            "remaining_owned_processes": remaining,
            "open_owned_ports": open_ports,
            "root_preserved": True,
            "vm_cleanup_verified": False,
            "complete": False,
        }
        self.evidence("abort-owned-services", receipt)
        assert inventory_error is None, (
            "process inventory incomplete; inspect preserved abort receipt"
        )
        assert not remaining, "owned processes remain; inspect preserved abort receipt"
        assert not open_ports, "owned ports remain; inspect preserved abort receipt"
        assert all(value in {"inactive", "failed", "unknown"} for value in status.values()), status

    def abort(self):
        """Explicitly abandon an incomplete synthetic lab and remove owned resources."""
        self.cleanup(incomplete=True)

    def cleanup(self, *, incomplete=False):
        """Always stop exact owned host units, retaining unresolved VM ownership."""
        if not incomplete:
            assert (self.path / "evidence/sage-final.json").exists(), (
                "collect final Sage evidence first"
            )
            assert not self.read("SELECT run FROM run_leases")
        try:
            self.cleanup_owned(incomplete=incomplete)
        except Exception as error:
            self.stop_owned_services(error)
            raise RuntimeError(
                "Full cleanup is incomplete; exact owned service shutdown was checked. "
                "The private lab root is retained for VM ownership and evidence inspection."
            ) from error

    def cleanup_owned(self, *, incomplete=False):
        """Destroy only ledger-owned resources, then export an allowlisted receipt."""
        units = [
            self.prefix + "-" + name + ".service"
            for name in ("launcher", "worker-probe", "model", "gateway", "middleware", "cache")
        ]
        processes = {}
        driver_directories: set[Path] = set()
        for unit in units:
            group = run(
                ["systemctl", "--user", "show", "--property=ControlGroup", "--value", unit],
                check=False,
            ).stdout.strip()
            if group:
                directory = Path("/sys/fs/cgroup") / group.lstrip("/")
                for file in directory.rglob("cgroup.procs"):
                    for text in file.read_text().splitlines():
                        pid = int(text)
                        try:
                            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                        except FileNotFoundError:
                            continue
                        processes[pid] = stat[19]
                        try:
                            command = Path(f"/proc/{pid}/cmdline").read_bytes().decode()
                        except FileNotFoundError:
                            continue
                        driver_directories.update(
                            Path(value)
                            for value in re.findall(
                                rf"/tmp/os-{os.getuid()}-[^/\s\x00,;]+",  # noqa: S108 - observe owned-process paths only
                                command,
                            )
                        )
        for unit in units[:3]:
            run(["systemctl", "--user", "stop", unit], check=False)
        listing = json.loads(self.cli("sandbox", "list", "-o", "json").stdout)
        assert not listing.get("next_page_token")
        owned = json.loads((self.path / "owned-sandboxes.json").read_text())
        current = {item["id"]: item["name"] for item in listing["sandboxes"]}
        assert current.items() <= owned.items(), "unrecorded sandbox: inspect ownership first"
        for identity, name in current.items():
            self.cli("sandbox", "delete", name, timeout=120)
            remaining = json.loads(self.cli("sandbox", "list", "-o", "json").stdout)
            assert not any(item["id"] == identity for item in remaining["sandboxes"])
        assert not json.loads(self.cli("sandbox", "list", "-o", "json").stdout)["sandboxes"]
        for unit in units[3:]:
            run(["systemctl", "--user", "stop", unit], timeout=120)

        def ports_closed():
            for port in self.ports.values():
                with socket.socket() as peer:
                    peer.settimeout(0.2)
                    if peer.connect_ex(("127.0.0.1", port)) == 0:
                        return False
            return True

        def processes_gone():
            for pid, started in processes.items():
                try:
                    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                except FileNotFoundError:
                    continue
                if stat[19] == started and stat[0] != "Z":
                    return False
            return True

        wait(ports_closed)
        wait(processes_gone)
        wait(lambda: all(not path.exists() for path in driver_directories))
        status = {
            unit: run(["systemctl", "--user", "is-active", unit], check=False).stdout.strip()
            for unit in units
        }
        assert not any(
            value in {"active", "activating", "deactivating"} for value in status.values()
        )
        self.evidence(
            "cleanup",
            {
                "owned_sandboxes": owned,
                "deleted_remaining": current,
                "gateway_empty": True,
                "ports_closed": True,
                "processes_gone": True,
                "driver_directory_count": len(driver_directories),
                "driver_directories_absent": True,
                "unit_status": status,
                "process_count": len(processes),
                "at": time.time(),
            },
        )
        observations = self.path / "model-observations.jsonl"
        if observations.exists():
            self.evidence(
                "models", [json.loads(line) for line in observations.read_text().splitlines()]
            )
        output = self.path.with_name(
            "ss-recovery-evidence-" + self.path.name.removeprefix("ss-recovery-")
        )
        output.mkdir(mode=0o700)
        hashes = {}
        for file in sorted((self.path / "evidence").glob("*.json")):
            text = file.read_text().replace(str(REPO), "$REPO").replace(str(self.path), "$LAB")
            assert not any(
                word in text for word in ("/home/", "Bearer ", "test-secret-", "PRIVATE KEY")
            )
            json.loads(text)
            private(output / file.name, text)
            hashes[file.name] = hashlib.sha256(text.encode()).hexdigest()
        # Recheck the same marker immediately before deleting only this lab root.
        assert json.loads((self.path / "identity.json").read_text()) == self.identity
        shutil.rmtree(self.path)
        assert not self.path.exists()
        private(
            output / "root-removed.json",
            json.dumps({"root_removed": True, "at": time.time()}) + "\n",
        )
        hashes["root-removed.json"] = hashlib.sha256(
            (output / "root-removed.json").read_bytes()
        ).hexdigest()
        private(output / "SHA256.json", json.dumps(hashes, indent=2) + "\n")
        print(output, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "prepare",
            "up",
            "start",
            "inspect",
            "tree",
            "literal",
            "automatic",
            "history",
            "narrative",
            "upload",
            "worker_cycle",
            "finish",
            "cleanup",
            "abort",
        ),
    )
    parser.add_argument("lab", nargs="?", type=Path)
    parser.add_argument("--duration", type=int, default=3600)
    parser.add_argument("--static", type=Path, default=REPO / "web/workspace/dist")
    args = parser.parse_args()
    if args.action == "prepare":
        assert 60 <= args.duration <= 7200
        print(prepare(args.duration, args.static))
        return
    if args.lab is None:
        parser.error("lab is required")
    getattr(Lab(args.lab), args.action)()


if __name__ == "__main__":
    main()
