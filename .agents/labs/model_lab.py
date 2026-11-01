# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Own a disposable real-model lab of the Phase A/B items (2026-10-31).

Run with the virtualenv of a fixed checkout: prepare, then up LAB, the steps, and
cleanup LAB (abort LAB after a failure). Only a marked /var/tmp/ss-modellab-* root is
accepted. It reuses recovery_acceptance.py's root, port, unit, gateway, TLS, replacement
and teardown machinery (``make_root``, ``Lab``, recovery_acceptance_up.sh) and
direct_mcp.py's portal URL reader.

``up`` brings up the lab's own gateway (VM driver), policy middleware with
``mcp_listings``, package cache, and the whole product through ``sandbox-science up``:
worker, control service, workspace and the agent runtime with ``[mcp_direct]`` on the
owner's portal (the provider registered by the launcher from the owner's Cloudflare Access
file, by its path alone). The model is the gateway's ``sandbox-orchestrator`` alias (an
``arrcodex`` alias, deliberately weaker; model errors are part of what is tested), for
every role; the gateway URL and the key file's path are read from the personal config at
``prepare`` and only the lab's private config names them; the key is never opened here.
``prepare --model ALIAS --api chat|responses|messages`` runs another alias, on the API the
launcher's ``[model] api`` names for it (``messages`` for a Claude alias on ``/v1/messages``,
2026-10-31).

Steps are small and composable, since a real model's turns are not scripted:

- ``send LAB SESSION STAGE``: posts the stage's prompt (``PROMPTS``) once;
- ``wait LAB SESSION``: waits until the session holds no run lease;
- ``api LAB METHOD PATH [JSON]``: one workspace API call as the browser makes it;
- ``sql LAB QUERY``: one read-only control-store query;
- ``delegation LAB ARGS``: ``sandbox-science-harness project delegation`` for the project;
- ``revision LAB``: accepts a classification revision of the portal's server mid-session;
- ``replace LAB SESSION``: deletes the runtime sandbox, replaces it with
  ``sandbox-science replace-orchestrator`` (whose output keeps the fence's note lines) and
  resumes the session, as recovery_acceptance.py's ``tree`` does;
- ``capture LAB SESSION NAME``: a sanitized receipt of the session's works, attempts,
  delegations and results, tool records (names, errors, paths), permission requests,
  check-ins and usage view, written as evidence ``NAME``.

Receipts are checked at export for the gateway's and the portal's addresses, home paths
and tokens. ``cleanup`` deletes the owned sandboxes, stops the units and removes the root.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
import tomllib
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from sandbox_science.launcher import toml
from sandbox_science.launcher.config import load_config
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.policy.store import Acceptance, PolicyStore


def _module(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


recovery = _module("recovery_acceptance", "recovery_acceptance.py")
REPO = recovery.REPO
OS_BIN = recovery.OS_BIN
PURPOSE = "disposable real-model Phase A/B lab"
PROJECT = "modellab"
SESSIONS = ("connectors", "review", "checkin", "kernel", "held", "delegate")
"""``held`` is the DA-20 lab's scripted session (no model, ``da20_probes.py``) and
``delegate`` its real-model session (stage ``delegate``)."""
MODEL = "sandbox-orchestrator"
SERVER = "crypto-research-mcp"
CONFIG = Path.home() / ".config/sandbox-science"
PERSONAL = CONFIG / "personal.toml"
CREDENTIALS = CONFIG / "secrets/cloudflare.env"
"""The owner's Cloudflare Access file: named in the lab's config, opened by the launcher's
helper only."""
PROFILE = REPO / "examples/openshell/profiles/cloudflare-access.yaml"
CATALOG_DIR = REPO / "packages/policy/src/sandbox_science/policy/classifications" / SERVER
ALLOWED_WRITE = "firecrawl-mcp_firecrawl_monitor_delete"
"""A ``write`` tool the lab policy allows by name: outward, unasked (refused by the child
fence under a draft plan). Called with a monitor ID that does not exist."""
ESCALATED_WRITE = "firecrawl-mcp_firecrawl_monitor_run"
"""A ``write`` tool the shipped class default escalates (grant ceiling project): it asks
first, so the fence lets it be held; a session grant then makes it unasked, refused."""
NOWHERE = "lab-nonexistent-0000"
REVISED = "e-print-mcp_corpus_stats"
"""The tool ``revision`` leaves out of the project's classification revision."""

PROMPTS: dict[str, str] = {
    "connectors": f"""This is a platform lab of delegated agents and connectors. Follow these steps exactly, in order, and do not do anything else.
1. Create a session plan with your plan tool: title "Connector lab", one step "Check the connector access of delegated agents". Leave it as a draft: do not approve, activate or start it.
2. Delegate a CHECK (a check delegation, not a task) whose question is: "Call mcp_tools to list every connector tool you are offered. Report the total number and list every tool name you were offered, grouped by the server prefix before -mcp_."
3. Delegate a TASK whose brief is: "Do these four steps in order and report each outcome exactly (the tool name and either success or the exact error text). Call each tool at most twice. (a) Call mcp_tools to list your connector tools and count them. (b) Call the connector tool {REVISED} with no arguments. (c) Call the connector tool {ALLOWED_WRITE} with the arguments {{"id": "{NOWHERE}"}}. (d) Call the connector tool {ESCALATED_WRITE} with the arguments {{"id": "{NOWHERE}"}}; if it says it is waiting for approval, wait up to five minutes, calling it again at most twice, then report."
4. Wait for both delegations to finish, then answer with their results verbatim.""",
    "connectors2": f"""The delegations were refused because the plan was a draft. The user now allows it: activate your session plan (do not change its text), then do exactly this:
1. Record a ledger entry, a claim: "The IACR ePrint corpus served by the e-print connector holds more than 1,000 papers." Then delegate a CHECK of that entry (its handle and revision), with the note: "First call mcp_tools and report how many connector tools you are offered and their names, grouped by server prefix; then use e-print-mcp_corpus_stats to check the claim."
2. Delegate a TASK whose brief is: "Do these steps in order and report each outcome exactly (the tool name and either success or the exact error text). Call each tool at most twice. (a) Call mcp_tools and count your connector tools. (b) Run the shell command sleep 120 (one call; wait for it). (c) Call the connector tool {REVISED} with no arguments. (d) Call the connector tool {ALLOWED_WRITE} with the arguments {{"id": "{NOWHERE}"}}. (e) Call the connector tool {ESCALATED_WRITE} with the arguments {{"id": "{NOWHERE}"}}; if it says it is waiting for approval, wait 60 seconds with a shell sleep and call it once more, then report."
3. Wait for both delegations to finish, then answer with their results verbatim.""",
    "connectors-grant": f"""Two more delegations, then stop.
1. Record the ePrint corpus size as a ledger claim with ledger_change: "The IACR ePrint corpus served by the e-print connector holds more than 1,000 papers", anchored to the earlier child's e-print-mcp_corpus_stats tool result (or to this message) as its evidence. Then delegate a CHECK of that entry (its handle and revision) with the note: "First call mcp_tools with a query such as 'firecrawl monitor eprint corpus' and report every connector tool you are offered or can load, by name; then check the claim with e-print-mcp_corpus_stats."
2. Delegate a TASK whose brief is: "Do these steps in order and report each outcome exactly (the tool name and either success or the exact error text). (a) Run the shell command sleep 120 (one call; wait for it). (b) Load and call the connector tool {ESCALATED_WRITE} with the arguments {{"id": "{NOWHERE}"}} once. (c) Call the connector tool {ALLOWED_WRITE} with the arguments {{"id": "{NOWHERE}"}} once. Do nothing else."
3. Wait for both, then answer with their results verbatim.""",
    "connectors3": f"""New instructions from the user, replacing the last ones.
1. Create a new session plan (title "Connector lab 2", one step "Grant and check probes") and activate it.
2. Record a claim with ledger_change, with changes = [{{"type": "claim", "statement": "The connector lab checks whether a delegated check is offered only read connector tools.", "basis": "user_quoted", "anchors": [{{"message": "<this user message's msg- ID, from its header>", "quote": "whether a delegated check is offered only read connector tools"}}]}}].
3. Delegate a CHECK of that entry (its handle, revision 1) with the note: "Call mcp_tools with the query 'firecrawl monitor', then with the query 'eprint corpus', and report every connector tool name you are offered or that loads."
4. Delegate a TASK whose brief is: "(a) Run the shell command sleep 120 (one call; wait for it). (b) Load {ESCALATED_WRITE} with mcp_tools (names) and call it with the arguments {{"id": "{NOWHERE}"}} once. Report the exact outcome." The user granted that write tool's class for this session; the earlier denial was of a different tool.
5. Wait for both, then answer with their results verbatim.""",
    "mcp": """This is a platform lab of connector calls. Do exactly these steps and nothing else.
1. Use mcp_tools to find and load the IACR ePrint corpus statistics tool, then call it once and say how many papers the corpus holds.
2. Use mcp_tools to find and load the NIST CSRC search tool, then call it once with the query "FIPS 203" and name the first publication it returns.
3. Delegate one TASK whose brief is: "Use mcp_tools to find and load the NIST CSRC search tool, call it once with the query SP 800-208, and report the title of the first result. Do nothing else." Wait for it, accept it with accept_delegation, then answer with the three results.""",
    "area": """Delegate one TASK whose brief is: "Write the file results/outside.md, at exactly that path relative to the project root (not under work/), containing the single line: outside first. If that write is refused, write the same file where the refusal says you may write, and report both outcomes exactly." Wait for its result, accept it with accept_delegation, then answer with the outcomes it reported.""",
    "checkin-seq": """Delegate one TASK whose brief is: "Start from x = 3. Run twelve python cells one after another, one cell per turn and never two tool calls at once: each cell sets x to (x*x + 1) % 1009 from the value the previous cell printed, and prints x. You cannot know a value before the previous cell has printed it. Then return the twelve values." Wait for it. If it checks in about its usage, decide it with resource_decide: extend it once. Then answer with its result.""",
    "review": """This is a platform lab of parent review of delegated tasks. Follow these steps exactly.
1. Delegate three TASKS at once, A, B and C. Brief A: "Write a file named alpha.md containing the single line: alpha first version. Then return." Brief B: "Write a file named notes/beta.md containing the single line: beta first version. Then return." Brief C: "Write a file named gamma.md containing the single line: gamma first version. Then return." Do not tell them where to write; they will find out.
2. Wait for all three results.
3. Review them: accept A with accept_delegation (note "A is fine"). Send B feedback with message_child: "Please edit the file you wrote earlier: append a second line, beta revised after review. Then return again." Do NOT accept or message C.
4. Wait for B's new result, then accept B with accept_delegation.
5. Finish with a short answer listing each task's file path and review state. Leave C unreviewed.""",
    "resume-child": """Delegate one TASK whose brief is: "Write the file delta.md containing the line: delta before the pause. Then run the shell command sleep 240 (one call, wait for it). Then edit delta.md you wrote earlier: append the line delta after the pause. Then return with the file's path." Wait for its result and accept it, then answer with the file's path.""",
    "checkin": """This is a platform lab of usage check-ins. Do this as separate steps, one tool call per step, and do not combine steps: run twenty separate python cells, the n-th of which prints n squared, for n from 1 to 20. After each cell, say in one short sentence what it printed. Then answer with the sum of the twenty values.""",
    "checkin-child": """Delegate one TASK whose brief is: "Run twelve separate python cells, one tool call each, the n-th printing 2 to the power n, for n from 1 to 12. Then return the twelve values." Wait for it. If it checks in about its usage, decide it with resource_decide: extend it once. Then answer with its result.""",
    "delegate": """This is a platform lab of delegated agents doing ordinary research work. Delegate three TASKS at once, then wait for all three results.
Brief A: "Use python cells (one tool call per cell). Cell 1: compute the primes below 20000 with a simple sieve and print how many there are. Cell 2: compute the sum of those primes and print it. Then run one shell command, ls -la, and report what it lists. Write a file results/a.json with the keys count and sum. Then return the file's path and the two numbers."
Brief B: "Compute the Smith normal form of the integer matrix [[2, 4, 4], [-6, 6, 12], [10, -4, -16]] and the factorization of x^8 - 1 over the integers, using the sympy package (install it if it is missing; do not write the algorithms yourself). Write both results to results/b.txt, then return the file's path and the results."
Brief C: "Start one background job (start_job) that runs this shell command: python3 -c 'import time, json; time.sleep(20); json.dump({"collatz_27": 111}, open("job-out.json", "w"))' and wait for it to finish. Then, with a python cell, read job-out.json, check the number 111 by computing the Collatz stopping time of 27 yourself, and write results/c.md with one line saying whether they agree. Return the file's path and that line."
When all three have returned, read each child's results file with read_file (at the path it reported), accept each task with accept_delegation, and answer with the three files' contents and paths.""",
    "delegate2": """Delegate one more TASK, D, and wait for its result. Brief D: "Using the networkx package (install it if it is missing; do not write the graph algorithms yourself), build the Petersen graph and compute its diameter, girth and number of spanning trees. Check the package version with one shell command that runs python3 and imports networkx. Write the three numbers to results/d.txt, then return the file's path and the numbers." When it returns, read its results file with read_file, accept it with accept_delegation, and answer with the file's contents and path.""",
    "kernel": """Compute, iteratively and checking each step, the first ten terms of the sequence a(1)=7, a(n+1)=(a(n)^2+1) mod 1000003, then the smallest n<=10 for which a(n) is divisible by 3, if any. Keep your intermediate state as you go, and answer with the ten terms and the result.""",
}


def prepare(static: Path, personal: Path, model: str = MODEL, api: str = "responses") -> Path:
    if not (static / "index.html").is_file():
        raise ValueError("build web/workspace first (npm run build), or pass --static")
    direct = _module("direct_mcp", "direct_mcp.py")
    images = {profile: recovery.image_recipe(profile) for profile in ("orchestrator", "minimal")}
    settings = tomllib.loads(personal.read_text())
    gateway_url = settings["model"]["url"]
    key_file = Path(settings["model"]["api_key_file"]).expanduser()
    assert key_file.is_absolute()
    portal = direct.portal_url(personal)
    lab, ports, values = recovery.make_root(
        "ss-modellab-",
        PURPOSE,
        ("gateway", "middleware", "cache", "unused", "worker", "workspace", "preview", "control"),
        {"images": images, "static": str(static.resolve()), "model": model, "api": api},
    )
    recovery.private(
        lab / "private.json", json.dumps({"gateway": gateway_url, "portal": portal}) + "\n"
    )
    policy = yaml.safe_load((REPO / "examples/policy.yaml").read_text())
    policy["metadata"] = {
        "name": "model-lab",
        "description": "The example on the owner's portal, one write tool allowed by name",
    }
    server = policy["mcp"]["servers"][SERVER]
    server["url"] = portal
    server["tools"]["allow"] = [*server["tools"].get("allow", []), ALLOWED_WRITE]
    recovery.private(lab / "policy.yaml", yaml.safe_dump(policy, sort_keys=False))
    example = REPO / "examples/sandbox-science.toml"
    lines = example.read_text().splitlines()
    start = lines.index("# [mcp_direct]")
    table = tomllib.loads(
        "\n".join(line.removeprefix("# ").removeprefix("#") for line in lines[start:])
    )["mcp_direct"]
    data = {
        "state_dir": str(lab / "instance"),
        "projects": [{"id": PROJECT, "profile": "minimal", "sessions": list(SESSIONS)}],
        "model": {
            "url": gateway_url,
            "name": model,
            "reviewer": model,
            "watcher": model,
            "delegated": model,
            "api": {model: api},
            "api_key_file": str(key_file),
        },
        "orchestrator": {
            "image": images["orchestrator"]["tag"],
            "revision": images["orchestrator"]["revision"],
            "control_listen": f"127.0.0.1:{ports['control']}",
        },
        "run_service": {"max_turns": 40},
        "worker": {
            "listen": f"127.0.0.1:{ports['worker']}",
            "pool_id": lab.name,
            "policy": str(lab / "policy.yaml"),
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
            "env": {key: values[key] for key in recovery.OPENSHELL_ENV},
            "environments": {
                "policy_store": str(lab / "middleware/state"),
                "targets": str(lab / "targets.yaml"),
                "ceiling": str(lab / "middleware/pool-ceiling.yaml"),
                "profiles": {"minimal": images["minimal"]["tag"]},
            },
        },
        "mcp_direct": {
            "providers": [SERVER],
            "servers": {
                SERVER: {
                    "credentials": {
                        "CF-Access-Client-Id": "CF_ACCESS_CLIENT_ID",
                        "CF-Access-Client-Secret": "CF_ACCESS_CLIENT_SECRET",
                    }
                }
            },
            "index": table["index"],
            "about": table["about"],
            "provider_env_file": str(CREDENTIALS),
            "provider_profile": str(PROFILE),
        },
    }
    recovery.private(lab / "sandbox-science.toml", toml.dumps(data))
    load_config(lab / "sandbox-science.toml")
    recovery.private(lab / "owned-sandboxes.json", "{}\n")
    return lab


class ModelLab(recovery.Lab):
    root_prefix = "ss-modellab-"
    evidence_prefix = "ss-modellab-evidence-"
    purpose = PURPOSE
    services = ("launcher", "worker-probe", "model")

    def private_words(self) -> tuple[str, ...]:
        path = self.path / "private.json"
        if not path.exists():
            return ()
        found = json.loads(path.read_text())
        words = []
        for url in found.values():
            words += [url, urlsplit(url).hostname or url]
        return tuple(words)

    def cleanup(self, *, incomplete=False):
        """The owned root's teardown; no run may hold a lease unless abandoning."""
        if not incomplete:
            assert not self.read("SELECT run FROM run_leases"), "a run still holds a lease"
        if (self.path / "gateway.toml").exists():
            # The worker creates the project's environment sandboxes after ``up``, on this
            # lab's own gateway: they are the lab's too (2026-10-31, Messages API lab).
            self.remember_sandboxes()
        recovery.OwnedRoot.cleanup(self, incomplete=incomplete)

    def harness(self, sub: str, *args: str, check=True):
        return recovery.run(
            [
                str(REPO / ".venv/bin/sandbox-science-harness"),
                "project",
                sub,
                *args,
                "--state-dir",
                str(self.path / "instance/control"),
                "--project",
                PROJECT,
            ],
            check=check,
        )

    def up(self):
        assert not (self.path / "gateway.toml").exists(), "already up"
        recovery.run(
            ["bash", str(REPO / ".agents/labs/recovery_acceptance_up.sh")],
            env={
                **self.env,
                "MIDDLEWARE_EXTRA": "mcp_listings = true",
                "LAB_POLICY": str(self.path / "policy.yaml"),
                "VM_MEM_MIB": "4096",
            },
            timeout=240,
        )
        self.cli(
            "provider", "profile", "import", "-f",
            REPO / "examples/openshell/profiles/github-token.yaml",
        )  # fmt: skip
        recovery.run(
            [
                str(OS_BIN / "openshell"), "provider", "create", "--name", "github-readonly",
                "--type", "github-token", "--credential", "GITHUB_TOKEN",
            ],
            env={**self.env, "GITHUB_TOKEN": "test-secret-modellab-github"},
        )  # fmt: skip
        out = self.path / "launcher.out"
        recovery.run(
            [
                "systemd-run", "--user", f"--unit={self.prefix}-launcher", "--collect", "-q",
                f"--working-directory={self.path}",
                "-p", f"StandardOutput=append:{out}", "-p", f"StandardError=append:{out}",
                str(REPO / ".venv/bin/sandbox-science"), "up", "--config", str(self.config),
            ],
            env=self.env,
        )  # fmt: skip
        try:
            recovery.wait(self.ready, seconds=600)
        finally:
            self.remember_sandboxes()
        self.harness("delegation", "--depth", "1")
        self.harness("budget", "--daily-cost-limit", "15")
        self.evidence(
            "ready",
            {
                "identity": {k: v for k, v in self.identity.items() if k != "source"},
                "fence_lines": self.fence_lines(),
            },
        )
        print("up", self.path, flush=True)

    def fence_lines(self) -> list[str]:
        out = self.path / "launcher.out"
        if not out.exists():
            return []
        return [
            line.strip()
            for line in out.read_text(errors="replace").splitlines()
            if "MCP egress" in line
        ]

    def post(self, session, action, body, *, timeout=30):
        status, found = self.api(
            "POST", f"/api/v0/projects/{PROJECT}/sessions/{session}/{action}", body
        )
        if status >= 400:
            raise RuntimeError(f"{action} refused {status}: {json.dumps(found)[:400]}")
        return found

    # Steps

    def send(self, session: str, stage: str) -> None:
        intent = self.path / f"message-intent-{session}-{stage}.json"
        assert not intent.exists(), "read the original message's status; do not replay it"
        body = {"message_id": f"modellab-{session}-{stage}", "text": PROMPTS[stage]}
        recovery.private(intent, json.dumps({"session": session, "stage": stage}) + "\n")
        self.login()
        admitted = self.post(session, "messages", body)
        self.evidence(f"sent-{session}-{stage}", {"admitted": admitted, "at": time.time()})
        print(json.dumps(admitted), flush=True)

    def leases(self, session: str) -> list:
        return self.read(
            "SELECT project_id,session_id,run FROM run_leases WHERE session_id=?", (session,)
        )

    def busy(self, session: str) -> bool:
        running = self.read(
            "SELECT count(*) FROM research_work WHERE session=? AND state IN ('running')",
            (session,),
        )[0][0]
        return bool(self.leases(session)) or bool(running)

    def wait_idle(self, session: str, seconds: float) -> None:
        """Until the session holds no lease and runs no work, twice 15 s apart."""
        end = time.monotonic() + seconds
        quiet = 0
        while time.monotonic() < end and quiet < 2:
            quiet = 0 if self.busy(session) else quiet + 1
            time.sleep(15)
        works = self.read(
            "SELECT state,reason,count(*) FROM research_work WHERE session=? GROUP BY 1,2",
            (session,),
        )
        print(json.dumps({"session": session, "idle": quiet >= 2, "works": works}), flush=True)

    def api(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        self.login()
        headers = {"Content-Type": "application/json"}
        if method != "GET":
            headers["Origin"] = self.base
        request = recovery.urllib.request.Request(
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
                return error.code, {"text": text[:400]}

    def revision(self) -> dict[str, Any]:
        """A project classification revision of the portal's server without ``REVISED``,
        accepted in the policy store the worker and middleware read, while sessions run."""
        tools: dict[str, Any] = {}
        for path in sorted(CATALOG_DIR.glob("*.yaml")):
            found = yaml.safe_load(path.read_text())
            tools.update(found["tools"])
        tools.pop(REVISED)
        data = yaml.safe_dump(
            {
                "apiVersion": "sandbox-science/v0alpha1",
                "kind": "ToolClassification",
                "server": SERVER,
                "tools": tools,
            },
            sort_keys=True,
            width=1_000_000,
        ).encode()
        accepted = PolicyStore(self.path / "middleware/state").accept_classification(
            PROJECT,
            SERVER,
            data,
            Acceptance("model-lab", datetime.now(UTC), f"lab: {REVISED} left out"),
        )
        value = {"revision": accepted.revision, "left_out": REVISED, "at": time.time()}
        self.evidence("revision", value)
        return value

    def replace(self, session: str) -> None:
        before = len(self.fence_lines())
        command = [str(REPO / ".venv/bin/sandbox-science"), "replace-orchestrator", "--config"]
        original = recovery.run
        captured: list[str] = []

        def run(args, **kwargs):
            result = original(args, **kwargs)
            if args[:2] == command[:2]:
                captured.extend((result.stdout + result.stderr).splitlines())
            return result

        recovery.run = run
        try:
            self.replacement(session, {"note": "model lab replacement"})
        finally:
            recovery.run = original
        self.evidence(
            f"{session}-replacement-fence",
            {
                "replace_output_fence_lines": [line for line in captured if "MCP egress" in line],
                "launcher_fence_lines_after": self.fence_lines()[before:],
            },
        )

    def capture(self, session: str, name: str) -> dict[str, Any]:
        from sandbox_science.orchestrator.context.history.operations import ToolJournal

        def rows(sql: str, args=()) -> list[dict[str, Any]]:
            projects = ProjectStore(self.path / "instance/control")
            try:
                with projects.control.read() as tx:
                    cursor = tx.sql.execute(sql, args)
                    names = [column[0] for column in cursor.description]
                    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
            finally:
                projects.close()

        records = rows(
            "SELECT message_id FROM archive_tool_records WHERE project=? AND session=? "
            "ORDER BY rowid",
            (PROJECT, session),
        )
        projects = ProjectStore(self.path / "instance/control")
        try:
            journal = ToolJournal(projects.control)
            tools = [journal.record(PROJECT, row["message_id"]) for row in records]
        finally:
            projects.close()
        value: dict[str, Any] = {
            "works": rows(
                "SELECT work,current,state,reason FROM research_work WHERE session=? ORDER BY work",
                (session,),
            ),
            "attempts": rows(
                "SELECT work,run,predecessor FROM research_attempts WHERE session=? ORDER BY run",
                (session,),
            ),
            "tools": [sanitize_tool(item) for item in tools if isinstance(item, dict)],
        }
        for table in ("delegations", "delegation_results"):
            try:
                value[table] = rows(f"SELECT * FROM {table} WHERE session_id=?", (session,))  # noqa: S608
            except Exception as error:  # a column name differs: keep what is known
                value[table] = {"error": type(error).__name__, "text": str(error)[:200]}
        for path in (
            f"/api/v0/projects/{PROJECT}/sessions/{session}/permission-requests",
            f"/api/v0/projects/{PROJECT}/sessions/{session}/usage/view",
            f"/api/v0/projects/{PROJECT}/sessions/{session}/resources",
            f"/api/v0/projects/{PROJECT}/sessions/{session}/tree",
            f"/api/v0/projects/{PROJECT}/grants",
        ):
            status, found = self.api("GET", path)
            value[
                path.rsplit("/", 1)[-1] if "grants" in path else path.split(session + "/", 1)[-1]
            ] = {
                "status": status,
                "body": found,
            }
        self.evidence(name, value)
        return value


def sanitize_tool(item: dict[str, Any]) -> dict[str, Any]:
    """A tool record's name, run, arguments' keys and outcome, never bodies over 600 chars."""
    keep: dict[str, Any] = {
        key: item.get(key)
        for key in ("tool", "run", "message_id", "reconciled", "purpose")
        if key in item
    }
    arguments = item.get("arguments")
    if isinstance(arguments, str):  # the journal keeps the call's arguments as JSON text
        try:
            arguments = json.loads(arguments)
        except ValueError:
            keep["arguments"] = arguments[:200]
    if isinstance(arguments, dict):
        keep["arguments"] = {k: str(v)[:200] for k, v in arguments.items()}
    for key in ("error", "result", "content", "output", "execution", "refusal", "effect"):
        if key in item:
            keep[key] = json.dumps(item[key], default=str)[:600]
    return keep


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "prepare", "up", "send", "wait", "api", "sql", "delegation", "revision",
            "replace", "capture", "fence", "cleanup", "abort",
        ),
    )  # fmt: skip
    parser.add_argument("lab", nargs="?", type=Path)
    parser.add_argument("rest", nargs="*")
    parser.add_argument("--static", type=Path, default=REPO / "web/workspace/dist")
    parser.add_argument("--personal", type=Path, default=PERSONAL)
    parser.add_argument("--seconds", type=float, default=1800)
    parser.add_argument("--model", default=MODEL, help="the gateway alias for every role")
    parser.add_argument("--api", default="responses", choices=("chat", "responses", "messages"))
    args = parser.parse_args()
    if args.action == "prepare":
        print(prepare(args.static, args.personal, args.model, args.api))
        return
    if args.lab is None:
        parser.error("lab is required")
    lab = ModelLab(args.lab)
    rest = args.rest
    if args.action == "up":
        lab.up()
    elif args.action == "send":
        lab.send(rest[0], rest[1])
    elif args.action == "wait":
        lab.wait_idle(rest[0], args.seconds)
    elif args.action == "api":
        body = json.loads(rest[2]) if len(rest) > 2 else None
        status, found = lab.api(rest[0], rest[1], body)
        print(status)
        print(json.dumps(found, indent=1, default=str))
    elif args.action == "sql":
        assert rest[0].lstrip().upper().startswith("SELECT"), "read-only queries only"
        for row in lab.read(rest[0], tuple(rest[1:])):
            print(json.dumps(row, default=str))
    elif args.action == "delegation":
        print(lab.harness("delegation", *rest).stdout)
    elif args.action == "revision":
        print(json.dumps(lab.revision()))
    elif args.action == "replace":
        lab.replace(rest[0])
    elif args.action == "capture":
        found = lab.capture(rest[0], rest[1])
        print(json.dumps(found, indent=1, default=str)[:20000])
    elif args.action == "fence":
        print("\n".join(lab.fence_lines()))
    elif args.action == "cleanup":
        lab.cleanup()
    else:
        lab.abort()


if __name__ == "__main__":
    main()
