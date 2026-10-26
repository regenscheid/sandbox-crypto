# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Own a disposable lab of the direct MCP path (D98, backlog A2) against the owner's portal.

Run with the checked-out virtualenv of a fixed checkout: prepare, then up LAB, proofs LAB
and cleanup LAB (abort LAB after a failure). Only a marked /var/tmp/ss-direct-mcp-* root
is accepted. It reuses recovery_acceptance.py's root, port, unit, gateway, TLS and
teardown machinery (``make_root``, ``OwnedRoot``, recovery_acceptance_up.sh).

``up`` brings up the lab's own gateway (VM driver), the policy middleware with
``mcp_listings``, a worker with a run-service client and an operator (workspace) client,
registers the MCP provider from the owner's Cloudflare Access file by its path alone
(``sandbox_science.launcher.access_provider``, as the launcher's ``provider_env_file``
does: only that helper and the one OpenShell command it hands the values to open it),
compiles the agent MCP endpoint for a lab policy over the portal's server
(``compile_agent_mcp``; classification from the checked-in crypto-research-mcp files, one
attack-catalog tool escalated and one denied), and creates an agent runtime sandbox from
the orchestrator image with the provider attached.

``proofs`` runs DirectClient in that sandbox (direct_mcp_agent.py) over one exec session,
whose standard streams carry the confinement Channel: the client's ControlRequestCheck asks
the lab host's ``confirm_request``, which binds the lab's project and asks the worker
(``confinement.requests.confirm`` with ``WorkerRequestCheck``, as the host's HostLoops
does). The lab answers a confirmed hold through the worker's answer route with the
operator token, as the workspace does. Proof 5 (binary results) is recorded as pending.

The portal's URL is read at run time from the personal instance's policy (named by
``~/.config/sandbox-science/personal.toml``), never written to the repository, and never
exported: receipts carry counts, reason codes, request numbers and tool names only.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import secrets
import sys
import time
import tomllib
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION
from sandbox_science.event_store import StoreReader
from sandbox_science.orchestrator.confinement.requests import confirm
from sandbox_science.orchestrator.confinement.transport import (
    Capability,
    Channel,
    TransportError,
)
from sandbox_science.orchestrator.mcp_direct import WorkerRequestCheck
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.policy import load_policy
from sandbox_science.policy.loader import load_ceiling
from sandbox_science.policy.openshell import (
    AgentMcpTarget,
    AnyOf,
    SandboxFilesystem,
    compile_agent_mcp,
)

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
PURPOSE = "disposable direct MCP lab against the owner's portal"
PROJECT = "directlab"
SERVER = "crypto-research-mcp"
CALLER = "agent-runtime"
SESSION = "lab-session"
RUN = "lab-run-1"
CONFIG = Path.home() / ".config/sandbox-science"
PERSONAL = CONFIG / "personal.toml"
CREDENTIALS = CONFIG / "secrets/cloudflare.env"
"""The owner's Cloudflare Access file: passed by path to access_provider, never opened here."""
PROFILE = REPO / "examples/openshell/profiles/cloudflare-access.yaml"
PYTHON = "/opt/sandbox-science/bin/python"
REMOTE = "/sandbox/lab"
ESCALATED = "attack-catalog-mcp_get_catalog_info"
DENIED = "attack-catalog-mcp_list_taxonomy"
INVALID = "attack-catalog-mcp_get_attack"
"""Called without its required ``attack_id``: the server's error answer."""
PAGE = "https://example.com/"
READS = [
    ["e-print-mcp_corpus_stats", {}],
    ["nist-csrc-mcp_nist_get_corpus_status", {}],
    ["web-fetch-mcp_web_fetch", {"url": PAGE, "max_chars": 2000}],
    ["firecrawl-mcp_firecrawl_scrape", {"url": PAGE, "formats": ["markdown"]}],
]
"""One read each on e-print, nist-csrc and web-fetch, and one small firecrawl scrape."""
RERUN_READS = [["portal_list_servers", {}]]
"""A later lab's allowed call: the portal's own listing of its servers, so that the reads
an earlier lab made upstream are not made again (``proofs --plan rerun``)."""
_RECORD_FIELDS = (
    "project",
    "caller",
    "session",
    "run",
    "server",
    "method",
    "tool",
    "outcome",
    "reason_code",
    "permission_request",
    "request",
    "scope",
    "source",
)


def portal_url(personal: Path) -> str:
    """The portal server's URL from the personal instance's policy, read at run time."""
    settings = tomllib.loads(personal.read_text())
    policy = Path(settings["worker"]["policy"]).expanduser()
    if not policy.is_absolute():
        policy = personal.parent / policy
    found = yaml.safe_load(policy.read_text())["mcp"]["servers"][SERVER]["url"]
    assert isinstance(found, str)
    assert urlsplit(found).scheme == "https"
    return found


def lab_policy(url: str) -> str:
    """The example policy, its portal server at ``url``, one attack-catalog tool escalated
    and one denied; every other tool's class comes from the checked-in classification."""
    policy = yaml.safe_load((REPO / "examples/policy.yaml").read_text())
    policy["metadata"]["name"] = "direct-mcp-lab"
    server = policy["mcp"]["servers"][SERVER]
    server["url"] = url
    server["tools"] = {
        "allow": ["portal_list_servers"],
        "escalate": [ESCALATED],
        "deny": [DENIED, "portal_*"],
    }
    return yaml.safe_dump(policy, sort_keys=False)


def prepare() -> Path:
    image = recovery.image_recipe("orchestrator")
    assert CREDENTIALS.parent.is_dir(), "the owner's secrets directory is missing"
    lab, _, _ = recovery.make_root(
        "ss-direct-mcp-",
        PURPOSE,
        ("gateway", "middleware", "cache", "unused", "worker"),
        {"images": {"orchestrator": image}},
    )
    recovery.private(lab / "owned-sandboxes.json", "{}\n")
    return lab


class DirectLab(recovery.OwnedRoot):
    root_prefix = "ss-direct-mcp-"
    evidence_prefix = "ss-direct-mcp-evidence-"
    purpose = PURPOSE
    services = ("worker",)
    infrastructure = ("gateway", "middleware", "cache")
    forbidden = (*recovery.OwnedRoot.forbidden, "openshell:resolve", "CF-Access-Client-Secret:")

    def private_words(self) -> tuple[str, ...]:
        path = self.path / "portal.json"
        if not path.exists():
            return ()
        url = json.loads(path.read_text())["url"]
        return (url, urlsplit(url).hostname or url)

    # Worker access, with the run service's or the workspace's token.

    def token(self, client: str) -> str:
        return (self.path / "worker" / f"{client}.token").read_text().strip()

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

    def sandbox_name(self) -> str:
        return json.loads((self.path / "agent-sandbox.json").read_text())["name"]

    # up

    def up(self, personal: Path):
        assert not (self.path / "gateway.toml").exists(), "already up"
        url = portal_url(personal)
        recovery.private(self.path / "portal.json", json.dumps({"url": url}) + "\n")
        recovery.run(
            ["bash", str(REPO / ".agents/labs/recovery_acceptance_up.sh")],
            env={**self.env, "MIDDLEWARE_EXTRA": "mcp_listings = true"},
            timeout=180,
        )
        # The provider, from the owner's file by its path: only the helper reads it.
        registered = recovery.run(
            [
                str(REPO / ".venv/bin/python"),
                "-I",
                "-m",
                "sandbox_science.launcher.access_provider",
                str(CREDENTIALS),
                "--profile",
                str(PROFILE),
                "--openshell",
                str(OS_BIN / "openshell"),
            ],
            env=self.env,
            timeout=180,
        )
        providers = json.loads(self.cli("provider", "list", "-o", "json").stdout)
        names = sorted(item.get("name") for item in providers.get("providers", []))
        assert SERVER in names, "the provider was not registered"
        recovery.private(self.path / "policy.yaml", lab_policy(url))
        loaded = load_policy(self.path / "policy.yaml")
        middleware = REPO / ".venv/bin/sandbox-science-policy-middleware"
        recovery.run(
            [
                str(middleware),
                "store",
                "--state-dir",
                str(self.path / "middleware/state"),
                "--policy",
                str(self.path / "policy.yaml"),
            ]
        )
        self.start_worker()
        compiled = self.compile(loaded)
        self.create_sandbox()
        self.evidence(
            "ready",
            {
                "identity": {k: v for k, v in self.identity.items() if k != "source"},
                "provider_registered": SERVER in names,
                "provider_helper_exit": registered.returncode,
                "policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest},
                "compiled": compiled,
                "sandbox": self.sandbox_name(),
            },
        )
        print("up", self.path, flush=True)

    def start_worker(self):
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
        image = self.identity["images"]["orchestrator"]["tag"]
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
        recovery.run(
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
        )
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

    def compile(self, loaded) -> dict[str, Any]:
        target = AgentMcpTarget(
            middleware="sandbox-science-policy",
            project=PROJECT,
            caller=CALLER,
            binaries=(PYTHON + "*",),
            filesystem=SandboxFilesystem(read_only=("/opt/sandbox-science",)),
        )
        compiled = compile_agent_mcp(
            loaded, target, ceiling=load_ceiling(self.path / "middleware/pool-ceiling.yaml")
        )
        recovery.private(self.path / "agent-policy.yaml", compiled.policy.to_yaml())
        (entry,) = compiled.policy.network_policies.values()
        (endpoint,) = entry.endpoints
        admitted = {
            name
            for rule in endpoint.rules or ()
            if rule.allow.method == "tools/call" and isinstance(rule.allow.tool, AnyOf)
            for name in rule.allow.tool.any
        }
        methods = sorted({rule.allow.method for rule in endpoint.rules or ()})
        assert compiled.policy.network_middlewares is not None
        config = dict(compiled.policy.network_middlewares["sandbox-science-policy"].config)
        return {
            "endpoint_protocol": endpoint.protocol,
            "credential_bound": endpoint.credential_binding is not None,
            "methods": methods,
            "admitted_tools": len(admitted),
            "escalated_admitted": ESCALATED in admitted,
            "denied_admitted": DENIED in admitted,
            "allowed_reads_admitted": all(tool in admitted for tool, _ in READS),
            "rerun_reads_admitted": all(tool in admitted for tool, _ in RERUN_READS),
            "deny_rules": len(endpoint.deny_rules or ()),
            "middleware_config_mcp": config.get("mcp"),
            "findings": [
                {"severity": str(finding.severity), "subject": finding.subject}
                for finding in compiled.findings
                if not finding.subject.startswith("hosts.")
            ],
        }

    def create_sandbox(self):
        name = "ss-dmcp-" + secrets.token_hex(3)
        recovery.private(self.path / "agent-sandbox.json", json.dumps({"name": name}) + "\n")
        image = self.identity["images"]["orchestrator"]["tag"]
        try:
            self.cli(
                "sandbox",
                "create",
                "--name",
                name,
                "--from",
                image,
                "--policy",
                self.path / "agent-policy.yaml",
                "--provider",
                SERVER,
                "--no-auto-providers",
                "--detach",
                "--",
                "sleep",
                "infinity",
                timeout=600,
            )
        finally:
            self.remember_sandboxes()

        def ready():
            found = json.loads(self.cli("sandbox", "get", name, "-o", "json").stdout)
            return found if found.get("phase") == "Ready" else None

        found = recovery.wait(ready, seconds=600)
        self.remember_sandboxes()
        # The middleware decides only sandboxes bound to a stored revision (as the worker
        # binds the research sandboxes it creates); nothing binds the agent's yet.
        recovery.run(
            [
                str(REPO / ".venv/bin/sandbox-science-policy-middleware"),
                "bind",
                "--state-dir",
                str(self.path / "middleware/state"),
                "--sandbox",
                found["id"],
                "--policy",
                str(self.path / "policy.yaml"),
            ]
        )
        shell = ["sandbox", "exec", "-n", name, "--no-tty", "--no-login-shell", "--"]
        self.cli(*shell, "mkdir", "-p", REMOTE)
        self.cli(
            "sandbox",
            "upload",
            "--no-git-ignore",
            name,
            REPO / ".agents/labs/direct_mcp_agent.py",
            REMOTE + "/direct_mcp_agent.py",
        )

    # proofs

    def proofs(self, plan: str):
        url = json.loads((self.path / "portal.json").read_text())["url"]
        number = len(list((self.path / "evidence").glob("proofs-*.json"))) + 1
        reads = READS if plan == "full" else RERUN_READS
        before = self.records()
        driven = asyncio.run(self.drive(url, reads, skip_error_answer=plan != "full"))
        records = self.records()[len(before) :]
        self.evidence(f"agent-steps-{number}", driven["steps"])
        self.evidence(f"records-{number}", records)
        self.judge(driven, records, reads, number, plan)

    async def drive(self, url: str, reads: list, *, skip_error_answer: bool) -> dict[str, Any]:
        # An attempt capability as the host issues them (wire.AttemptId).
        attempt = "cap-" + secrets.token_hex(24)
        worker = WorkerClient(f"http://127.0.0.1:{self.ports['worker']}", self.token("harness"))
        check = WorkerRequestCheck(worker)
        process = await asyncio.create_subprocess_exec(
            str(OS_BIN / "openshell"),
            "sandbox", "exec", "-n", self.sandbox_name(), "--no-tty", "--no-login-shell", "--",
            PYTHON, "-I", REMOTE + "/direct_mcp_agent.py",
            "--url", url, "--project", PROJECT, "--session", SESSION, "--run", RUN,
            "--attempt", attempt, "--escalated", ESCALATED, "--denied", DENIED,
            "--invalid-tool", INVALID, "--reads", json.dumps(reads),
            *(["--skip-error-answer"] if skip_error_answer else []),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self.env, limit=32 * 1024 * 1024,
        )  # fmt: skip
        assert process.stdin is not None
        assert process.stdout is not None
        stdin = process.stdin
        confirmations: list[dict[str, Any]] = []
        cards: list[dict[str, Any]] = []
        steps: list[dict[str, Any]] = []
        held: list[dict[str, Any]] = []
        tasks: list[asyncio.Task[None]] = []

        async def send(text: str) -> None:
            stdin.write((json.dumps({"t": "frame", "text": text}) + "\n").encode())
            await stdin.drain()

        async def handle(kind: Capability, value: Any) -> Any:
            # The host side of the runtime's confirm_request: the lab's one attempt, its
            # project bound here, never one the runtime names.
            if kind is not Capability.CONFIRM_REQUEST or value.get("attempt") != attempt:
                raise TransportError("denied", "not this attempt's capability")
            asked_at = time.time()
            reply = await confirm(check, PROJECT, value)
            confirmations.append(
                {
                    "request": value.get("request"),
                    "reason": value.get("reason"),
                    "tool": value.get("tool"),
                    "method": value.get("method"),
                    "confirmed": reply["confirmed"],
                    "asked_at": asked_at,
                    "answered_at": time.time(),
                }
            )
            return reply

        channel = Channel(send, handle)

        async def answer(request: int) -> None:
            # As the workspace: list the project's requests, find the card, approve it.
            listing = await asyncio.to_thread(
                self.worker, "GET", f"/v1/permission-requests?project={PROJECT}", operator=True
            )
            seen_at = time.time()
            found = [item for item in listing["requests"] if item["seq"] == request]
            card: dict[str, Any] = {"request": request, "listed": len(found), "seen_at": seen_at}
            if found:
                item = found[0]
                mcp = item.get("mcp") or {}
                card.update(
                    kind=item["kind"],
                    caller_kind=item["caller_kind"],
                    session=item.get("session_id"),
                    run=item.get("run"),
                    max_grant=item["max_grant"],
                    reason=item.get("reason"),
                    server=mcp.get("server"),
                    method=mcp.get("method"),
                    tool=mcp.get("tool"),
                    effect=mcp.get("effect"),
                    summary_fields=len(mcp.get("summary") or {}),
                )
                answered = await asyncio.to_thread(
                    self.worker,
                    "POST",
                    f"/v1/permission-requests/{request}/answer",
                    {"approve": True, "scope": "call"},
                    operator=True,
                )
                card.update(answer_kind=answered["kind"], answered_at=time.time())
            cards.append(card)

        async def pump() -> None:
            assert process.stdout is not None
            while line := await process.stdout.readline():
                message = json.loads(line)
                if message["t"] == "frame":
                    await channel.receive(message["text"])
                elif message["t"] == "held":
                    held.append(message)
                    tasks.append(asyncio.ensure_future(answer(int(message["request"]))))
                elif message["t"] == "step":
                    steps.append(message)
                elif message["t"] == "done":
                    break

        try:
            await asyncio.wait_for(pump(), timeout=900)
            for task in tasks:
                await task
        finally:
            stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=30)
            except TimeoutError:
                process.kill()
                await process.wait()
            assert process.stderr is not None
            said = await process.stderr.read()
            recovery.private(self.path / "agent-stderr.txt", said.decode("utf-8", "replace"))
            await channel.close()
            await worker.close()
        return {
            "steps": steps,
            "confirmations": confirmations,
            "cards": cards,
            "held": held,
            "exit_code": process.returncode,
        }

    def records(self) -> list[dict[str, Any]]:
        """The middleware's records, with whitelisted fields only (never arguments)."""
        found = []
        with StoreReader(self.path / "middleware/state/events.db") as reader:
            for record in reader.records():
                data = record.data()
                kept: dict[str, Any] = {"seq": record.seq, "kind": record.kind}
                for key in _RECORD_FIELDS:
                    value = data.get(key)
                    if key in data and (isinstance(value, str | int | bool) or value is None):
                        kept[key] = value
                decision = data.get("decision")
                if isinstance(decision, dict):
                    kept["decision"] = {
                        key: decision.get(key)
                        for key in ("action", "layer", "effect", "max_grant")
                        if key in decision
                    }
                for key in ("added", "removed", "changed"):
                    if isinstance(data.get(key), list):
                        kept[key + "_count"] = len(data[key])
                found.append(kept)
        return found

    def judge(
        self,
        driven: dict[str, Any],
        records: list[dict[str, Any]],
        planned: list,
        number: int,
        plan: str,
    ) -> None:
        steps = {step["step"]: step for step in driven["steps"] if step["step"] != "read"}
        reads = [step for step in driven["steps"] if step["step"] == "read"]
        calls = [r for r in records if r["kind"] == "mcp-call"]

        def tool_calls(tool):
            return [r for r in calls if r.get("tool") == tool]

        listing = steps.get("listing", {})
        environment = steps.get("environment", {})
        escalated = steps.get("escalated", {})
        error_answer = steps.get("error_answer", {})
        denied = steps.get("denied", {})
        answers = steps.get("answers", {}).get("answers", [])
        first = escalated.get("first") or {}
        request = first.get("request")
        confirmations = [c for c in driven["confirmations"] if c["request"] == request]
        cards = [c for c in driven["cards"] if c["request"] == request]
        escalation_calls = tool_calls(ESCALATED)
        proof1 = {
            "denied_refused_by": denied.get("error"),
            "denied_reached_middleware": len(tool_calls(DENIED)),
            "allowed_reads_passed": sum(1 for step in reads if step.get("ok")),
            "allowed_reads": len(reads),
            "allowed_read_outcomes": [
                {"tool": step["tool"], "ok": step.get("ok"), "error": step.get("error")}
                for step in reads
            ],
            "escalated_reached_middleware": len(escalation_calls),
        }
        proof1["pass"] = (
            denied.get("ok") is True
            and proof1["denied_reached_middleware"] == 0
            and proof1["allowed_reads_passed"] == len(planned)
            and proof1["escalated_reached_middleware"] >= 1
        )
        card = cards[0] if cards else {}
        proof2 = {
            "first_refusal": first.get("error"),
            "request": request,
            "confirmations": [
                {k: c[k] for k in ("request", "reason", "tool", "confirmed")} for c in confirmations
            ],
            "confirmed_before_card": bool(
                confirmations
                and card
                and confirmations[0]["confirmed"] is True
                and confirmations[0]["answered_at"] <= card["seen_at"]
            ),
            "card": {k: v for k, v in card.items() if not k.endswith("_at")},
            "seconds_confirm_to_answer": (
                round(card["answered_at"] - confirmations[0]["answered_at"], 3)
                if confirmations and card.get("answered_at")
                else None
            ),
            "attempts": escalated.get("attempts"),
            "retried_call_passed": escalated.get("ok"),
            "middleware_outcomes": [
                {
                    "outcome": r.get("outcome"),
                    "reason_code": r.get("reason_code"),
                    "permission_request": r.get("permission_request"),
                    "session": r.get("session"),
                    "run": r.get("run"),
                    "project": r.get("project"),
                }
                for r in escalation_calls
            ],
            "record_kinds": sorted({r["kind"] for r in records}),
        }
        proof2["pass"] = bool(
            proof2["first_refusal"] == "approval_required"
            and isinstance(request, int)
            and proof2["confirmed_before_card"]
            and card.get("answer_kind") == "approval"
            and escalated.get("ok") is True
        )
        listing_answers = [a for a in answers if a.get("method") == "tools/list"]
        identity = steps.get("listing_identity")
        proof3 = {
            "listing_media": sorted({a["media"] for a in listing_answers}),
            "listing_answers": listing_answers,
            "listing": {k: v for k, v in listing.items() if k not in ("t", "step")},
            "listing_identity": (
                {k: v for k, v in identity.items() if k not in ("t", "step")}
                if identity is not None
                else None
            ),
            "other_answer_heads": sorted(
                {
                    json.dumps(
                        {
                            k: a.get(k)
                            for k in (
                                "media",
                                "content_encoding",
                                "cache_control",
                                "content_length",
                            )
                        },
                        sort_keys=True,
                    )
                    for a in answers
                    if a.get("method") != "tools/list"
                }
            ),
            "drift_records": [r for r in records if r["kind"] == "mcp-tools-drift"],
            "error_answer": {k: v for k, v in error_answer.items() if k not in ("t", "step")},
            "answer_media": sorted({a["media"] for a in answers}),
        }
        proof3["pass"] = bool(
            listing.get("ok")
            and "text/event-stream" in proof3["listing_media"]
            and listing.get("denied_listed") is False
            and listing.get("escalated_marked") is True
            and listing.get("marked_with_effect") == listing.get("tools")
            and (error_answer.get("ok") is True or error_answer.get("skipped") is True)
        )
        found = identity or {}
        proof3["pass_with_identity_encoding"] = bool(
            found.get("ok")
            and found.get("denied_listed") is False
            and found.get("escalated_marked") is True
            and found.get("marked_with_effect") == found.get("tools")
        )
        variables = environment.get("variables", {})
        upstream = bool(
            listing.get("ok")
            or (identity or {}).get("ok")
            or proof1["allowed_reads_passed"] > 0
            or escalated.get("ok")
        )
        proof4 = {
            "variables": variables,
            "upstream_calls_succeeded": upstream,
            "credential_bound_in_policy": True,
        }
        proof4["pass"] = bool(
            variables
            and all(v["present"] and v["placeholder"] for v in variables.values())
            and upstream
        )
        result = {
            "proof_1_native_rules": proof1,
            "proof_2_middleware_hold": proof2,
            "proof_3_response_binding": proof3,
            "proof_4_placeholders": proof4,
            "proof_5_binary_results": {
                "pass": None,
                "status": "pending: the runtime-to-host binary capability is another lane's",
            },
            "agent_exit_code": driven["exit_code"],
            "held_reports": len(driven["held"]),
            "run": number,
            "plan": plan,
        }
        self.evidence(f"proofs-{number}", result)
        self.evidence("proofs", result)
        for name in (
            "proof_1_native_rules",
            "proof_2_middleware_hold",
            "proof_3_response_binding",
            "proof_4_placeholders",
        ):
            print(("PASS " if result[name]["pass"] else "FAIL ") + name, flush=True)
        print("PENDING proof_5_binary_results", flush=True)
        print(json.dumps(result, indent=2, sort_keys=True), flush=True)

    def before_export(self):
        # A last check of what leaves: no portal address, no placeholder value.
        for file in (self.path / "evidence").glob("*.json"):
            text = file.read_text()
            assert not re.search(r"https?://(?!127\.0\.0\.1)", text.replace(PAGE, "")), file.name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "up", "proofs", "cleanup", "abort"))
    parser.add_argument("lab", nargs="?", type=Path)
    parser.add_argument("--personal", type=Path, default=PERSONAL)
    parser.add_argument(
        "--plan",
        choices=("full", "rerun"),
        default="full",
        help="rerun: the portal's own listing as the allowed call and no error answer, so a "
        "later lab does not repeat an earlier lab's upstream reads",
    )
    args = parser.parse_args()
    if args.action == "prepare":
        print(prepare())
        return
    if args.lab is None:
        parser.error("lab is required")
    lab = DirectLab(args.lab)
    if args.action == "up":
        lab.up(args.personal)
    elif args.action == "proofs":
        lab.proofs(args.plan)
    elif args.action == "cleanup":
        assert (lab.path / "evidence/proofs.json").exists(), "run proofs first, or use abort"
        lab.cleanup()
    else:
        lab.abort()


if __name__ == "__main__":
    main()
