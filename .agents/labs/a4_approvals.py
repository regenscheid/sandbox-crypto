# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Own a disposable lab of the workspace answering network and package requests (D97, A4).

Run with the virtualenv of a fixed checkout: prepare, then up LAB, cards LAB (one network
and one package card left waiting, for screenshots), answers LAB, and cleanup LAB (abort
LAB after a failure). Only a marked /var/tmp/ss-a4lab-* root is accepted. It reuses
recovery_acceptance.py's root, port, unit, gateway, TLS and teardown machinery
(``make_root``, ``OwnedRoot``, recovery_acceptance_up.sh with ``LAB_POLICY``).

``up`` brings up the lab's own gateway (VM driver), policy middleware and package cache,
a worker with a run-service client and an operator (workspace) client, a project with
sessions s1 and s2 in its default environment and s3 in one of its own, and the
workspace server on loopback with the worker connection the launcher gives it. No agent
runtime and no model: requests are driven as research code, by shell executions the
lab submits through the worker as the run service would, and by provisioning operations
(``POST /v1/provisioning``) as the agent's ``provision`` tool would. Every answer goes
through the workspace's own HTTP API, logged in with its printed token, same origin.

The lab policy is ``examples/policy.yaml`` without its MCP server and credential, with
GETs to example.com, example.org and www.example.com escalated (an exact-host rule beats
``read-only-web``) and the pypi ecosystem's files escalated. POSTs escalate by the
example's ``external-writes`` rule. Receipts keep status codes, reason codes, request
numbers, offered scopes, summaries' shapes and grant rows, never bodies or tokens.
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import importlib.util
import json
import re
import secrets
import shlex
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION
from sandbox_science.policy import load_policy

_spec = importlib.util.spec_from_file_location(
    "recovery_acceptance", Path(__file__).with_name("recovery_acceptance.py")
)
assert _spec is not None
assert _spec.loader is not None
recovery = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = recovery
_spec.loader.exec_module(recovery)

REPO = recovery.REPO
OS_BIN = recovery.OS_BIN
PURPOSE = "disposable A4 approvals lab: workspace answers to network and package requests"
PROJECT = "a4lab"
SHARED = ("s1", "s2")  # sessions in the project's default environment
DEDICATED = "s3"  # a session with an environment (a sandbox) of its own
PYTHON = "/opt/sandbox-science/bin/python"
ONCE_HOST = "example.com"
GRANT_HOST = "example.org"
CARD_HOST = "www.example.com"
WRITE_HOST = "example.net"
REASONS = (
    "approval_required",
    "approval_denied",
    "escalation_timed_out",
    "policy_denied",
    "destination_forbidden",
    "request_unclassifiable",
    "audit_unavailable",
    "policy_stale",
    "sandbox_unbound",
)
# Pure wheels without dependencies, one per probe, so that no probe's files cover another's.
PACKAGES = {
    "card": "packaging==25.0",  # s2's provisioning, left waiting for its card, then denied
    "code_once": "six==1.17.0",  # research code's own download, allowed once
    "session": "idna==3.10",  # s1's provisioning, granted for the session
    "session_relayed": "iniconfig==2.1.0",  # s1 again: the session grant answers it
    "project": "attrs==25.3.0",  # s2: not covered by s1's grant; granted for the project
    "code_relayed": "tomli==2.2.1",  # research code's download: the project grant answers it
}


def lab_policy() -> str:
    policy = yaml.safe_load((REPO / "examples/policy.yaml").read_text())
    policy["metadata"] = {
        "name": "a4-lab",
        "description": "The example, with three hosts' reads and PyPI files escalated",
    }
    policy.pop("mcp", None)
    policy.pop("credentials", None)
    policy["packages"]["pypi"]["action"] = "escalate"
    policy["network"]["rules"].insert(
        1,
        {
            "name": "lab-escalated-reads",
            "operation": "http",
            "match": {"hosts": [ONCE_HOST, GRANT_HOST, CARD_HOST], "methods": ["GET", "HEAD"]},
            "action": "escalate",
        },
    )
    return yaml.safe_dump(policy, sort_keys=False)


def prepare(static: Path) -> Path:
    if not (static / "index.html").is_file():
        raise ValueError("build web/workspace first (npm run build), or pass --static")
    image = recovery.image_recipe("minimal")
    lab, _, _ = recovery.make_root(
        "ss-a4lab-",
        PURPOSE,
        ("gateway", "middleware", "cache", "worker", "workspace", "preview"),
        {"images": {"minimal": image}, "static": str(static.resolve())},
    )
    recovery.private(lab / "owned-sandboxes.json", "{}\n")
    recovery.private(lab / "policy.yaml", lab_policy())
    return lab


def reason_of(text: str) -> str | None:
    return next((code for code in REASONS if code in text), None)


class A4Lab(recovery.OwnedRoot):
    root_prefix = "ss-a4lab-"
    evidence_prefix = "ss-a4lab-evidence-"
    purpose = PURPOSE
    services = ("workspace", "worker")
    infrastructure = ("gateway", "middleware", "cache")

    def __init__(self, path: Path):
        super().__init__(path)
        self.base = f"http://127.0.0.1:{self.ports['workspace']}"
        self.opener: urllib.request.OpenerDirector | None = None

    def private_words(self) -> tuple[str, ...]:
        words = [self.token(client) for client in ("harness", "workspace", "worker")]
        login = self.path / "workspace.out"
        if login.exists():
            words += re.findall(r"token=([A-Za-z0-9_-]{20,})", login.read_text())
        return tuple(word for word in words if word)

    # Access to the worker (as the run service) and the workspace (as the browser)

    def token(self, client: str) -> str:
        path = self.path / "worker" / f"{client}.token"
        return path.read_text().strip() if path.exists() else ""

    def worker(self, method, path, body=None, *, operator=False, timeout=60):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.ports['worker']}" + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.token("workspace" if operator else "harness"),
                PROTOCOL_HEADER: str(PROTOCOL_VERSION),
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.load(response)

    def login(self):
        if self.opener is not None:
            return
        out = self.path / "workspace.out"
        recovery.wait(lambda: out.exists() and "token=" in out.read_text(), seconds=60)
        url = re.search(r"http://\S+/login\?token=[A-Za-z0-9_-]+", out.read_text())
        assert url is not None, "the workspace printed no login URL"
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self.opener.open(url.group(0), timeout=10).close()

    def api(self, method, path, body=None) -> tuple[int, Any]:
        """The workspace's API as the browser calls it: the session cookie and, for a
        change, the server's own Origin."""
        self.login()
        assert self.opener is not None
        headers = {"Content-Type": "application/json"}
        if method != "GET":
            headers["Origin"] = self.base
        request = urllib.request.Request(  # noqa: S310 - fixed owned loopback URL
            self.base + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        try:
            with self.opener.open(request, timeout=60) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            text = error.read().decode(errors="replace")
            try:
                return error.code, json.loads(text)
            except ValueError:
                return error.code, {"text": text[:200]}

    def cards(self, session: str) -> list[dict[str, Any]]:
        status, value = self.api(
            "GET", f"/api/v0/projects/{PROJECT}/sessions/{session}/permission-requests"
        )
        assert status == 200, (status, value)
        return value["requests"]

    def answer(self, card: dict[str, Any], decision: str, hours=None) -> tuple[int, Any]:
        return self.api(
            "POST",
            f"/api/v0/projects/{PROJECT}/permission-requests/{card['seq']}/answer",
            {"decision": decision, "kind": card["kind"], "source": "worker", "hours": hours},
        )

    def grants(self) -> list[dict[str, Any]]:
        status, value = self.api("GET", f"/api/v0/projects/{PROJECT}/grants")
        assert status == 200, (status, value)
        return value["grants"]

    # up

    def state(self) -> dict[str, Any]:
        return json.loads((self.path / "lab-state.json").read_text())

    def save_state(self, value: dict[str, Any]):
        recovery.private(self.path / "lab-state.json", json.dumps(value, indent=2) + "\n")

    def up(self):
        assert not (self.path / "gateway.toml").exists(), "already up"
        recovery.run(
            ["bash", str(REPO / ".agents/labs/recovery_acceptance_up.sh")],
            env={**self.env, "LAB_POLICY": str(self.path / "policy.yaml"), "VM_MEM_MIB": "4096"},
            timeout=240,
        )
        loaded = load_policy(self.path / "policy.yaml")
        digest = self.start_worker()
        assert digest == loaded.digest, (digest, loaded.digest)
        harness = str(REPO / ".venv/bin/sandbox-science-harness")
        control = str(self.path / "instance/control")
        scope = ["--state-dir", control, "--project", PROJECT]
        recovery.run(
            [harness, "project", "create", *scope, "--profile", "minimal", "--policy", digest]
        )
        for session in SHARED:
            recovery.run([harness, "session", "open", *scope, "--session", session])
        recovery.run([harness, "session", "open", *scope, "--session", DEDICATED, "--dedicated"])
        shown = json.loads(recovery.run([harness, "project", "show", *scope]).stdout)
        environments = {
            "default": shown["default_environment_id"],
            DEDICATED: shown["sessions"][DEDICATED]["environment_id"],
        }
        admissions = {}
        try:
            for name, environment in environments.items():
                self.worker(
                    "POST",
                    "/v1/environments",
                    {
                        "environment_id": environment,
                        "project_id": PROJECT,
                        "profile": "minimal",
                        "policy": digest,
                    },
                    timeout=900,
                )
                found = self.worker(
                    "POST",
                    f"/v1/environments/{environment}/admissions",
                    {
                        "hosts": [
                            {"host": host, "port": 443}
                            for host in (ONCE_HOST, GRANT_HOST, CARD_HOST, WRITE_HOST)
                        ]
                    },
                    timeout=300,
                )
                admissions[name] = {item["host"]: item["admitted"] for item in found["decisions"]}
        finally:
            self.remember_sandboxes()
        self.save_state({"environments": environments, "policy": digest})
        self.start_workspace()
        self.login()
        self.evidence(
            "ready",
            {
                "identity": {k: v for k, v in self.identity.items() if k != "source"},
                "policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest},
                "environments": sorted(environments),
                "admissions": admissions,
                "workspace_login": True,
            },
        )
        print("up", self.path, flush=True)

    def start_worker(self) -> str:
        directory = self.path / "worker"
        directory.mkdir(mode=0o700)
        command = str(REPO / ".venv/bin/sandbox-science-worker")
        recovery.run([command, "token", "--out", str(directory / "worker.token")])
        lines = {
            client: recovery.run(
                [command, "token", "--out", str(directory / f"{client}.token")]
            ).stdout.strip()
            for client in ("harness", "workspace")
        }
        image = self.identity["images"]["minimal"]["tag"]
        env = ", ".join(f'{key} = "{value}"' for key, value in self.openshell_env().items())
        recovery.private(
            directory / "worker.toml",
            f'''listen = "127.0.0.1:{self.ports["worker"]}"
token_file = "worker.token"
state_dir = "state"
pool_id = "{self.prefix}"

[openshell]
cli = "{OS_BIN / "openshell"}"
python = "{PYTHON}"
isolation_tier = 3
package_cache = "http://host.openshell.internal:{self.ports["cache"]}"
env = {{{env}}}

[openshell.environments]
policy_store = "{self.path / "middleware/state"}"
targets = "{self.path / "targets.yaml"}"
ceiling = "{self.path / "middleware/pool-ceiling.yaml"}"
profiles = {{minimal = "{image}"}}

[[clients]]
name = "harness"
{lines["harness"]}
projects = ["{PROJECT}"]

[[clients]]
name = "workspace"
{lines["workspace"]}
projects = ["{PROJECT}"]
operator = true
''',
        )
        added = recovery.run(
            [
                command,
                "policy",
                "add",
                "--settings",
                str(directory / "worker.toml"),
                "--project",
                PROJECT,
                str(self.path / "policy.yaml"),
            ],
            timeout=180,
        ).stdout
        digest = re.search(r"sha256:[0-9a-f]{64}", added)
        assert digest is not None, "policy add printed no digest"
        self.unit(
            "worker",
            [command, "serve", "--settings", directory / "worker.toml", "--log-level", "info"],
            cwd=directory,
        )

        def listening():
            try:
                return self.worker(
                    "GET", f"/v1/permission-requests?project={PROJECT}", operator=True
                )
            except OSError:
                return None

        recovery.wait(listening, seconds=60)
        return digest.group(0)

    def start_workspace(self):
        out = self.path / "workspace.out"
        recovery.run(
            [
                "systemd-run",
                "--user",
                f"--unit={self.prefix}-workspace",
                "--collect",
                "-q",
                f"--working-directory={self.path}",
                "-p",
                f"StandardOutput=file:{out}",
                str(REPO / ".venv/bin/sandbox-science-workspace"),
                "serve",
                "--state-dir",
                str(self.path / "instance/control"),
                "--static",
                self.identity["static"],
                "--listen",
                f"127.0.0.1:{self.ports['workspace']}",
                "--preview-listen",
                f"127.0.0.1:{self.ports['preview']}",
                "--worker",
                f"http://127.0.0.1:{self.ports['worker']}",
                "--worker-token-file",
                str(self.path / "worker/workspace.token"),
            ],
            env=self.env,
        )

    # Driving research code and provisioning through the worker

    def environment(self, session: str) -> str:
        environments = self.state()["environments"]
        return environments[DEDICATED] if session == DEDICATED else environments["default"]

    def context(self, session: str) -> dict[str, str]:
        return {
            "project_id": PROJECT,
            "session_id": session,
            "environment_id": self.environment(session),
        }

    def shell(self, session: str, script: str, *, timeout=180) -> dict[str, Any]:
        operation = "a4-" + secrets.token_hex(6)
        started = self.worker(
            "POST",
            "/v1/executions",
            {
                "kind": "shell",
                "operation_id": operation,
                "context": self.context(session),
                "script": script,
                "timeout_seconds": timeout,
            },
        )
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.ports['worker']}/v1/executions/"
            f"{started['execution_id']}/events?after=0",
            headers={
                "Authorization": "Bearer " + self.token("harness"),
                PROTOCOL_HEADER: str(PROTOCOL_VERSION),
            },
        )
        with urllib.request.urlopen(request, timeout=timeout + 60) as response:  # noqa: S310
            for line in response:
                if line.strip():
                    event = json.loads(line)
                    if event.get("type") == "result":
                        return event["execution"]
        raise RuntimeError("no result for " + started["execution_id"])

    def fetch(self, session: str, url: str, *, method="GET", body=None) -> dict[str, Any]:
        """Research code's request from the session's sandbox: its status and the policy's
        reason code when refused; never the body."""
        code = (
            "import json,sys,urllib.request,urllib.error\n"
            "url,method,body=sys.argv[1],sys.argv[2],sys.argv[3]\n"
            "data=body.encode() if body else None\n"
            "headers={'User-Agent':'a4-lab/1','Accept':'text/html'}\n"
            "headers.update({'Content-Type':'application/json'} if data else {})\n"
            "request=urllib.request.Request(url,method=method,data=data,headers=headers)\n"
            "try:\n"
            "    with urllib.request.urlopen(request,timeout=30) as r:\n"
            "        out={'status':r.status,'body':r.read(4000).decode(errors='replace')}\n"
            "except urllib.error.HTTPError as e:\n"
            "    out={'status':e.code,'body':e.read(4000).decode(errors='replace')}\n"
            "except Exception as e:\n"
            "    out={'status':None,'body':type(e).__name__+': '+str(e)[:400]}\n"
            "print(json.dumps(out))\n"
        )
        payload = json.dumps(body) if body is not None else ""
        script = (
            f"{PYTHON} -I -c \"import base64;exec(base64.b64decode('"
            f"{base64.b64encode(code.encode()).decode()}'))\" {shlex.quote(url)} {method} "
            f"{shlex.quote(payload)}"
        )
        done = self.shell(session, script)
        stdout = ((done.get("result") or {}).get("stdout") or {}).get("text") or ""
        try:
            value = json.loads(stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"status": None, "reason": None, "ran": done.get("state")}
        # OpenShell's refusal (``middleware_denied``) is not the upstream server's own 403.
        return {
            "status": value["status"],
            "reason": reason_of(value["body"]),
            "policy_refusal": "middleware_denied" in value["body"],
        }

    def pip_download(self, session: str, spec: str) -> dict[str, Any]:
        """Research code's own pip download through the package cache (no provisioning)."""
        directory = "/tmp/a4-" + secrets.token_hex(4)  # noqa: S108 - inside the sandbox
        done = self.shell(
            session,
            f"{PYTHON} -I -m pip download --no-deps --only-binary :all: -d {directory} "
            f"'{spec}' 2>&1 | tail -n 5; echo exit=${{PIPESTATUS[0]}}",
            timeout=300,
        )
        text = ((done.get("result") or {}).get("stdout") or {}).get("text") or ""
        found = re.search(r"exit=(\d+)", text)
        return {
            "exit": int(found.group(1)) if found else None,
            "reason": reason_of(text),
            "status_403": "403" in text,
        }

    def provision(self, session: str, spec: str) -> str:
        found = self.worker(
            "POST",
            "/v1/provisioning",
            {
                "operation_id": "a4-prv-" + secrets.token_hex(6),
                "context": self.context(session),
                "purpose": f"A4 lab: install {spec.split('==')[0]} to exercise a package card",
                "pip": [spec],
                "pip_reason": "a lab probe of PyPI's escalated files",
            },
        )
        return found["provision_id"]

    def provisioning(self, provision: str, *, until, seconds=600) -> dict[str, Any]:
        deadline = time.monotonic() + seconds
        seq = -1
        while True:
            value = self.worker(
                "GET", f"/v1/provisioning/{provision}?after={seq}&wait=20", timeout=60
            )
            seq = value.get("seq", seq)
            if until(value) or time.monotonic() > deadline:
                return value

    @staticmethod
    def operation(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": value.get("state"),
            "outcome": value.get("outcome"),
            "reason": value.get("reason"),
            "stage": value.get("stage"),
            "permission_request": value.get("permission_request"),
            "files": len(value.get("files") or ()),
            "changes": len(value.get("changes") or ()),
        }

    # Waiting for cards

    def card_for(self, session: str, match, *, seconds=90, retry=None) -> dict[str, Any]:
        """The card ``match`` picks, once the session lists it. ``retry`` repeats the request
        every 5 s meanwhile: a first try can fail before its host's admission is loaded, and
        retries of a recorded request are not recorded again."""
        found: list[dict[str, Any]] = []
        last = [time.monotonic()]

        def look():
            found[:] = [card for card in self.cards(session) if match(card)]
            if not found and retry is not None and time.monotonic() - last[0] > 5:
                retry()
                last[0] = time.monotonic()
            return bool(found)

        recovery.wait(look, seconds=seconds)
        return found[-1]

    @staticmethod
    def http_card(host: str, path: str, method: str = "GET"):
        def match(card):
            http = card.get("http") or {}
            return (
                card["kind"] == "network"
                and http.get("host") == host
                and http.get("path") == path
                and http.get("method") == method
            )

        return match

    @staticmethod
    def package_card(seq: int):
        return lambda card: card["seq"] == seq

    @staticmethod
    def shape(card: dict[str, Any]) -> dict[str, Any]:
        """A card's shape for the receipt: no payload, and no agent's words."""
        http = card.get("http") or {}
        packages = card.get("packages") or {}
        return {
            "seq": card["seq"],
            "kind": card["kind"],
            "source": card.get("source"),
            "caller_kind": card.get("caller_kind"),
            "max_grant": card.get("max_grant"),
            "scopes": card.get("scopes"),
            "session": card.get("session"),
            "rule": card.get("rule"),
            "reason": card.get("reason"),
            "on_timeout": card.get("on_timeout"),
            "http": {
                "method": http.get("method"),
                "host": http.get("host"),
                "port": http.get("port"),
                "path": http.get("path"),
                "size": http.get("size"),
                "content_type": http.get("content_type"),
                "fields": sorted((http.get("fields") or {}).keys()),
            }
            if http
            else None,
            "packages": {
                "sources": packages.get("sources"),
                "files": packages.get("files"),
                "has_purpose": bool(packages.get("purpose") or packages.get("asked")),
            }
            if packages
            else None,
        }

    @staticmethod
    def answered(status: int, value: Any) -> dict[str, Any]:
        if status != 200:
            return {"status": status, "error": (value or {}).get("error", {}).get("kind")}
        return {
            "status": status,
            "decision": value.get("decision"),
            "granted": [
                {key: grant.get(key) for key in ("kind", "scope", "session", "host", "ecosystem")}
                for grant in value.get("granted") or ()
            ],
        }

    # The probes

    def raise_cards(self):
        """One network card and one package card, left waiting for screenshots."""
        url = f"https://{CARD_HOST}/a4-card"
        refused = self.fetch("s1", url)
        network = self.card_for(
            "s1", self.http_card(CARD_HOST, "/a4-card"), retry=lambda: self.fetch("s1", url)
        )
        provision = self.provision("s2", PACKAGES["card"])
        waiting = self.provisioning(
            provision, until=lambda v: v.get("state") in ("awaiting_permission", "finished")
        )
        assert waiting["state"] == "awaiting_permission", self.operation(waiting)
        package = self.card_for("s2", self.package_card(waiting["permission_request"]))
        in_s1 = [card["seq"] for card in self.cards("s1")]
        in_s3 = [card["seq"] for card in self.cards(DEDICATED)]
        state = self.state()
        state["cards"] = {
            "network": network["seq"],
            "package": package["seq"],
            "provision": provision,
        }
        self.save_state(state)
        self.evidence(
            "cards",
            {
                "network_first_try": refused,
                "network_card": self.shape(network),
                "package_operation": self.operation(waiting),
                "package_card": self.shape(package),
                "network_card_in_every_session": network["seq"] in in_s1
                and network["seq"] in in_s3,
                "package_card_only_in_its_session": package["seq"] not in in_s1,
            },
        )
        print(json.dumps({"network": network["seq"], "package": package["seq"]}), flush=True)

    def answers(self):
        state = self.state()
        result: dict[str, Any] = {}

        # Deny: the two cards raised for the screenshots.
        network = self.card_for("s1", lambda c: c["seq"] == state["cards"]["network"])
        package = self.card_for("s2", lambda c: c["seq"] == state["cards"]["package"])
        deny_network = self.answer(network, "deny")
        deny_package = self.answer(package, "deny")
        denied_op = self.provisioning(
            state["cards"]["provision"], until=lambda v: v.get("state") == "finished"
        )
        result["deny_get"] = {
            "answer": self.answered(*deny_network),
            "retry": self.fetch("s1", f"https://{CARD_HOST}/a4-card"),
            "retry_later": self.fetch("s1", f"https://{CARD_HOST}/a4-card"),
            "card_gone": network["seq"] not in [c["seq"] for c in self.cards("s1")],
        }
        result["deny_package"] = {
            "answer": self.answered(*deny_package),
            "operation": self.operation(denied_op),
            "card_gone": package["seq"] not in [c["seq"] for c in self.cards("s2")],
        }

        # Once: a GET to example.com.
        first = self.fetch("s1", f"https://{ONCE_HOST}/")
        card = self.card_for(
            "s1",
            self.http_card(ONCE_HOST, "/"),
            retry=lambda: self.fetch("s1", f"https://{ONCE_HOST}/"),
        )
        given = self.answer(card, "once")
        other_first = self.fetch("s1", f"https://{ONCE_HOST}/a4-other")
        other = self.card_for("s1", self.http_card(ONCE_HOST, "/a4-other"))
        result["once_get"] = {
            "first_try": first,
            "card": self.shape(card),
            "answer": self.answered(*given),
            "retry": self.fetch("s1", f"https://{ONCE_HOST}/"),
            "identical_later": self.fetch("s1", f"https://{ONCE_HOST}/"),
            "other_path_first": other_first,
            "other_path_new_request": other["seq"] != card["seq"],
            "grants_after": len(self.grants()),
        }
        self.answer(other, "deny")

        # Project: a GET to example.org, then other paths, then from another sandbox.
        first = self.fetch("s1", f"https://{GRANT_HOST}/")
        card = self.card_for(
            "s1",
            self.http_card(GRANT_HOST, "/"),
            retry=lambda: self.fetch("s1", f"https://{GRANT_HOST}/"),
        )
        session_try = self.answer(card, "session")  # no session: not offered, and refused
        given = self.answer(card, "project")
        retry = self.fetch("s1", f"https://{GRANT_HOST}/")
        other_path = self.fetch("s1", f"https://{GRANT_HOST}/a4-other")
        other_sandbox_first = self.fetch(DEDICATED, f"https://{GRANT_HOST}/")
        relayed_at = time.monotonic()
        retries = []
        for _ in range(10):
            time.sleep(4)
            got = self.fetch(DEDICATED, f"https://{GRANT_HOST}/")
            retries.append(got)
            if got["status"] is not None and not got["policy_refusal"]:
                break
        pending = [
            c["seq"]
            for c in self.cards(DEDICATED)
            if (c.get("http") or {}).get("host") == GRANT_HOST
        ]
        result["project_get"] = {
            "first_try": first,
            "card": self.shape(card),
            "session_answer_refused": self.answered(*session_try),
            "answer": self.answered(*given),
            "retry": retry,
            "other_path": other_path,
            "other_sandbox_first": other_sandbox_first,
            "other_sandbox_retries": retries,
            "other_sandbox_seconds": round(time.monotonic() - relayed_at, 1),
            "other_sandbox_card_left": pending,
        }

        # Deny a write: a POST with a JSON body to example.net; wider scopes are refused.
        body = {"title": "a4 lab", "note": "x" * 300, "count": 3, "nested": {"a": [1, 2]}}
        first = self.fetch("s1", f"https://{WRITE_HOST}/a4-post", method="POST", body=body)
        card = self.card_for(
            "s1",
            self.http_card(WRITE_HOST, "/a4-post", "POST"),
            retry=lambda: self.fetch(
                "s1", f"https://{WRITE_HOST}/a4-post", method="POST", body=body
            ),
        )
        project_try = self.answer(card, "project")
        session_try = self.answer(card, "session")
        given = self.answer(card, "deny")
        result["deny_post"] = {
            "first_try": first,
            "card": self.shape(card),
            "project_answer_refused": self.answered(*project_try),
            "session_answer_refused": self.answered(*session_try),
            "answer": self.answered(*given),
            "retry": self.fetch("s1", f"https://{WRITE_HOST}/a4-post", method="POST", body=body),
        }

        # Once: research code's own pip download of a PyPI file.
        first = self.pip_download("s1", PACKAGES["code_once"])
        card = self.card_for(
            "s1", lambda c: c["kind"] == "packages" and (c.get("http") or {}).get("host")
        )
        given = self.answer(card, "once")
        result["once_code_package"] = {
            "first_try": first,
            "card": self.shape(card),
            "answer": self.answered(*given),
            "retry": self.pip_download("s1", PACKAGES["code_once"]),
        }

        # Session: s1's provisioning of PyPI files, then another in s1 the grant answers.
        provision = self.provision("s1", PACKAGES["session"])
        waiting = self.provisioning(
            provision, until=lambda v: v.get("state") in ("awaiting_permission", "finished")
        )
        card = self.card_for("s1", self.package_card(waiting["permission_request"]))
        given = self.answer(card, "session")
        done = self.provisioning(provision, until=lambda v: v.get("state") == "finished")
        second = self.provision("s1", PACKAGES["session_relayed"])
        second_done = self.provisioning(second, until=lambda v: v.get("state") == "finished")
        result["session_package"] = {
            "operation_waiting": self.operation(waiting),
            "card": self.shape(card),
            "answer": self.answered(*given),
            "operation": self.operation(done),
            "relayed_operation": self.operation(second_done),
        }

        # Project: s2's provisioning is not covered by s1's session grant; granted for the
        # project, it then covers research code's own download of another PyPI file.
        provision = self.provision("s2", PACKAGES["project"])
        waiting = self.provisioning(
            provision, until=lambda v: v.get("state") in ("awaiting_permission", "finished")
        )
        time.sleep(10)  # three relay rounds: s1's session grant must not answer it
        still = self.worker("GET", f"/v1/provisioning/{provision}")
        card = self.card_for("s2", self.package_card(waiting["permission_request"]))
        given = self.answer(card, "project")
        done = self.provisioning(provision, until=lambda v: v.get("state") == "finished")
        code_first = self.pip_download("s1", PACKAGES["code_relayed"])
        retries = []
        for _ in range(6):
            time.sleep(4)
            got = self.pip_download("s1", PACKAGES["code_relayed"])
            retries.append(got)
            if got["exit"] == 0:
                break
        result["project_package"] = {
            "operation_waiting": self.operation(waiting),
            "still_waiting_after_10s": still.get("state"),
            "card": self.shape(card),
            "answer": self.answered(*given),
            "operation": self.operation(done),
            "code_download_first": code_first,
            "code_download_retries": retries,
        }

        # The Grants panel's data, and a revocation.
        listed = self.grants()
        result["grants"] = [
            {
                key: grant.get(key)
                for key in (
                    "kind",
                    "scope",
                    "session",
                    "host",
                    "methods",
                    "ecosystem",
                    "channel",
                    "uses",
                    "expires_at",
                )
            }
            | {"last_used": grant.get("last_used_at") is not None}
            for grant in listed
        ]
        session_grant = next(
            g for g in listed if g["kind"] == "packages" and g["scope"] == "session"
        )
        status, after = self.api(
            "POST", f"/api/v0/projects/{PROJECT}/grants/{session_grant['grant_id']}/revoke"
        )
        result["revoke"] = {
            "status": status,
            "remaining": len((after or {}).get("grants") or ()),
            "revoked_listed": any(
                g["grant_id"] == session_grant["grant_id"]
                for g in (after or {}).get("grants") or ()
            ),
        }
        result["pending_left"] = {
            s: [c["seq"] for c in self.cards(s)] for s in (*SHARED, DEDICATED)
        }
        self.evidence("answers", result)
        print(json.dumps(result, indent=2), flush=True)


def recheck(lab: A4Lab):
    """The project grant's effect again, with the fixed fetch: the first lab's fetch sent
    urllib's default User-Agent, which example.org itself refuses with a 403 of its own."""
    rows = {
        "s1_same_path": lab.fetch("s1", f"https://{GRANT_HOST}/"),
        "s1_other_path": lab.fetch("s1", f"https://{GRANT_HOST}/a4-other"),
        "s3_relayed_path": lab.fetch(DEDICATED, f"https://{GRANT_HOST}/"),
        "s3_new_path": lab.fetch(DEDICATED, f"https://{GRANT_HOST}/a4-third"),
        "once_host_other_path_denied": lab.fetch("s1", f"https://{ONCE_HOST}/a4-other"),
    }
    lab.evidence("project-recheck", rows)
    print(json.dumps(rows, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("prepare", "up", "cards", "answers", "recheck", "cleanup", "abort")
    )
    parser.add_argument("lab", nargs="?", type=Path)
    parser.add_argument("--static", type=Path, default=REPO / "web/workspace/dist")
    args = parser.parse_args()
    if args.action == "prepare":
        print(prepare(args.static))
        return
    if args.lab is None:
        parser.error("lab is required")
    lab = A4Lab(args.lab)
    if args.action == "up":
        lab.up()
    elif args.action == "cards":
        lab.raise_cards()
    elif args.action == "answers":
        lab.answers()
    elif args.action == "recheck":
        recheck(lab)
    elif args.action == "cleanup":
        assert (lab.path / "evidence/answers.json").exists(), "run answers first, or use abort"
        lab.cleanup()
    else:
        lab.abort()


if __name__ == "__main__":
    main()
