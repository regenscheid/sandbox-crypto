# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Prepare and operate only the new, explicitly approved scientific-quality lab.

No live mode is the default. ``selftest`` is offline and uses scripted models.
``prepare`` creates private configuration only. ``authorize`` records an operator
authorization and a key FILE PATH, never reads it, and still starts no service.
Only ``up`` and subsequent message stages can dispatch paid calls after approval.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import secrets
import shlex
import socket
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError

# The two adjacent, tracked lab modules have no import-time effects. Isolated
# Python otherwise omits this script's directory from its import path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import recovery_acceptance as recovery
import scientific_quality_oracle as oracle
from scientific_quality_checks import (
    CASES,
    REFERENCE,
    SESSION,
    export_records,
    grade_result,
    seed_case,
    selftest,
)

from sandbox_science.launcher import toml
from sandbox_science.launcher.config import ModelConfig
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState

SOL = "arrcodex-gpt-6-sol"
LUNA = "arrcodex-gpt-6-luna"
PURPOSE = "inverse-sbox scientific-quality evaluation"
PLAN = Path(__file__).with_name("scientific-quality-plan.md")
PROMPTS = {
    "initial": "Initial root message",
    "child": "Child continuity instruction, delivered through the parent mailbox",
    "changed": "Assumption change after at least two accepted child folds",
    "resumed": "After the controlled interruption and one root Resume",
}
CALIBRATION_MESSAGE = (
    "This session contains a fixture-authored draft and a recorded calculation. A consistency "
    "review is queued. Do not edit or independently assess the draft before that reviewer "
    "returns. State that you are ready for its review at this checkpoint. If the built-in "
    "reviewer raises a finding, check its pinned calculation, correct the draft if warranted, "
    "and respond through the finding tool with the corrected revision. Do not confirm or "
    "dismiss your own finding. If it finds no issue, preserve the draft. Report the actual "
    "review and correction status, including incomplete coverage."
)


def prompt(stage: str) -> str:
    section = PLAN.read_text().split("### " + PROMPTS[stage] + "\n", 1)[1].split("\n##", 1)[0]
    return "\n".join(
        line[2:] if line.startswith("> ") else ""
        for line in section.splitlines()
        if line.startswith(">")
    )


def private_json(path: Path, value: object) -> None:
    recovery.private(path, json.dumps(value, indent=2) + "\n")


def stop_units(units: list[str], command=recovery.run) -> dict:
    """Try every exact owned unit even when a prior stop fails or times out."""
    receipts = {}
    for unit in units:
        try:
            result = command(["systemctl", "--user", "stop", unit], timeout=30, check=False)
            receipts[unit] = {"exit_code": result.returncode}
        except (OSError, TimeoutError, recovery.subprocess.TimeoutExpired) as error:
            receipts[unit] = {"error_type": type(error).__name__}
    return receipts


def live_model(key_file: Path) -> dict:
    """The key is an opaque absolute path consumed only by the normal launcher."""
    assert key_file.is_absolute()
    value = {
        "url": "https://litellm-api.npages.org/v1",
        "name": SOL,
        "delegated": SOL,
        "reviewer": LUNA,
        "api_key_file": str(key_file),
        "api": {SOL: "responses", LUNA: "responses"},
        "reasoning_effort": dict.fromkeys(
            ("main", "delegated", "reviewer", "fold", "overview"), "max"
        ),
        "review_token_budget": 400000,
        "review_calls_per_hour": 120,
    }
    ModelConfig.model_validate(value)
    assert tomllib.loads(toml.dumps({"model": value}))["model"] == value
    return value


def prepare(static: Path) -> Path:
    """Reuse the qualified random-root, image-pin and ownership machinery."""
    root = recovery.prepare(60, static)
    config = json.loads(
        json.dumps(
            recovery.load_config(root / "sandbox-science.toml").model_dump(
                mode="json", exclude_none=True
            )
        )
    )
    config["projects"][0]["sessions"] = [SESSION, *CASES]
    config["run_service"]["max_turns"] = 200
    recovery.private(root / "sandbox-science.toml", toml.dumps(config))
    assignment = [True, False]
    secrets.SystemRandom().shuffle(assignment)
    private_json(
        root / "quality.json",
        {
            "purpose": PURPOSE,
            "mode": "prepared",
            "created": datetime.now(UTC).isoformat(),
            "prompt_sha256": hashlib.sha256(PLAN.read_bytes()).hexdigest(),
            "cases": dict(zip(CASES, assignment, strict=True)),
            "receipt_marker": "quality-receipt-" + secrets.token_hex(16),
        },
    )
    return root


class Lab(recovery.Lab):
    def __init__(self, path: Path):
        super().__init__(path)
        self.quality_path = self.path / "quality.json"
        self.quality = json.loads(self.quality_path.read_text())
        assert self.quality["purpose"] == PURPOSE
        assert self.quality["prompt_sha256"] == hashlib.sha256(PLAN.read_bytes()).hexdigest(), (
            "prepared prompt changed"
        )

    def authorize(self, key_file: Path, allowance: float, note: str):
        """Record the owner's separate allowance; do not consume the referenced key."""
        assert self.quality["mode"] == "prepared"
        assert allowance == 150, "the owner authorized USD 150 aggregate new test spend"
        assert note.strip(), "record the owner's explicit authorization text"
        assert key_file.is_absolute()
        config = recovery.load_config(self.config).model_dump(mode="json", exclude_none=True)
        config["model"] = live_model(key_file)
        recovery.private(self.config, toml.dumps(config))
        recovery.load_config(self.config)
        self.quality.update(
            mode="authorized",
            allowance=allowance,
            admission_stop=130.0,
            authorization=note,
            authorized=datetime.now(UTC).isoformat(),
        )
        private_json(self.quality_path, self.quality)

    def up(self):
        assert self.quality["mode"] == "authorized", "owner authorization required"
        marker = self.path / "up-attempt.json"
        assert not marker.exists(), "inspect the prior startup outcome before retrying"
        private_json(marker, {"attempted": time.time()})
        super().up()
        recovery.run(
            [
                str(recovery.REPO / ".venv/bin/sandbox-science-harness"),
                "project",
                "delegation",
                "--state-dir",
                str(self.path / "instance/control"),
                "--project",
                recovery.PROJECT,
                "--depth",
                "2",
                "--task-tokens",
                "20000000",
            ]
        )
        projects = ProjectStore(self.path / "instance/control")
        try:
            RunState(projects).set_cost_limit(recovery.PROJECT, self.quality["admission_stop"])
        finally:
            projects.close()
        # The qualified kit's synthetic model unit is unused by live profiles.
        # Reuse its owned unit identity for the estimate guard, so cleanup owns it.
        recovery.run(["systemctl", "--user", "stop", self.prefix + "-model.service"])
        self.unit(
            "model",
            [
                recovery.REPO / ".venv/bin/python",
                "-I",
                Path(__file__).resolve(),
                "guard",
                self.path,
            ],
        )
        self.quality["mode"] = "running"
        private_json(self.quality_path, self.quality)
        self.evidence(
            "quality-ready",
            {
                "revision": self.identity["revision"],
                "profiles": {
                    "researcher": SOL,
                    "reviewer": LUNA,
                    "effort": "max",
                    "api": "responses",
                },
                "allowance": self.quality["allowance"],
                "admission_stop": self.quality["admission_stop"],
            },
        )

    def budget(self) -> dict:
        projects = ProjectStore(self.path / "instance/control")
        try:
            runs = RunState(projects)
            return {
                "observed": runs.model_cost(recovery.PROJECT),
                "reserved": sum(runs.model_reservations(recovery.PROJECT).values()),
                "unpriced_usage": runs.unpriced_calls(
                    recovery.PROJECT, datetime(1970, 1, 1, tzinfo=UTC)
                ),
                "unsettled_intents": self.read(
                    "SELECT count(*) FROM research_model_calls WHERE project=? AND usage IS NULL",
                    (recovery.PROJECT,),
                )[0][0],
                "daily_cap": runs.cost_limit(recovery.PROJECT),
                "at": time.time(),
            }
        finally:
            projects.close()

    def guard(self):
        warned = False
        while True:
            value = self.budget()
            self.evidence("quality-budget", value)
            known = (value["observed"] or 0) + value["reserved"]
            if known >= 100 and not warned:
                print(
                    "Quality lab reached USD 100 known plus reserved; inspect progress and unknowns.",
                    flush=True,
                )
                warned = True
            if known >= self.quality["admission_stop"]:
                projects = ProjectStore(self.path / "instance/control")
                try:
                    RunState(projects).set_cost_limit(recovery.PROJECT, 0)
                finally:
                    projects.close()
                self.evidence("quality-budget-stop", value)
                return
            time.sleep(5)

    def ensure_live(self):
        assert self.quality["mode"] == "running"
        status = recovery.run(
            ["systemctl", "--user", "is-active", self.prefix + "-model.service"], check=False
        ).stdout.strip()
        assert status == "active", "inspect the estimate guard before any paid admission"
        assert self.ready(), "owned runtime is not ready"
        cost = self.budget()
        assert cost["daily_cap"] is not None
        assert cost["daily_cap"] > 0
        assert (cost["observed"] or 0) + cost["reserved"] < self.quality["admission_stop"]
        self.login()

    def send_once(self, session: str, stage: str, text: str):
        self.ensure_live()
        intent = self.path / ("message-intent-" + stage + ".json")
        assert not intent.exists(), (
            "read the original message's status; do not replay an unknown admission"
        )
        body = {"message_id": "quality-" + self.path.name + "-" + stage, "text": text}
        private_json(intent, {"session": session, **body})
        admitted = self.post(session, "messages", body)
        self.evidence("quality-message-" + stage, admitted)

    def message(self, stage: str):
        if stage == "changed":
            # The precise accepted child fold check is done by inspect(), rather
            # than treating a model call or an overflow marker as an applied fold.
            inspection = self.inspect()
            assert inspection["applied_child_folds"] >= 2, inspection["applied_child_folds"]
        self.send_once(SESSION, stage, prompt(stage))

    def context(self, session: str) -> dict:
        rows = self.read(
            "SELECT environment_id FROM sessions WHERE project_id=? AND session_id=?",
            (recovery.PROJECT, session),
        )
        assert len(rows) == 1
        return {
            "project_id": recovery.PROJECT,
            "session_id": session,
            "environment_id": rows[0][0],
            "actor": "user",
        }

    def calibrate(self, session: str):
        """Generate worker evidence; let the real reviewer and author decide next."""
        assert session in CASES
        self.ensure_live()
        assert not self.read("SELECT run FROM run_leases"), (
            "settle the preceding calibration or study first"
        )
        marker = self.path / (session + "-seed-intent.json")
        assert not marker.exists(), (
            "inspect original execution and document receipts before retrying"
        )
        operation = "reference-" + session
        private_json(marker, {"operation_id": operation, "session": session})
        admitted = self.worker(
            "POST",
            "/v1/executions",
            {
                "kind": "shell",
                "context": self.context(session),
                "operation_id": operation,
                "script": "python3 -c " + shlex.quote(REFERENCE),
                "timeout_seconds": 60,
                "output_limit_bytes": 16384,
            },
            operator=True,
        )
        self.evidence(session + "-reference-admitted", admitted)
        execution = admitted["execution_id"]

        def terminal():
            receipt = self.worker(
                "GET", f"/v1/executions/{execution}?project_id={recovery.PROJECT}", operator=True
            )
            return (
                receipt
                if receipt["state"] in {"succeeded", "failed", "cancelled", "timed_out", "lost"}
                else None
            )

        receipt = recovery.wait(terminal, seconds=90)
        assert receipt["state"] == "succeeded", receipt
        assert receipt["result"]["exit_code"] == 0
        stdout = receipt["result"]["stdout"]["text"]
        observed = json.loads(stdout)
        expected = oracle.measurements(oracle.Field(4, 0x13))
        assert observed["inverse_table"] == expected["inverse_table"]
        assert observed["differential_uniformity"] == expected["differential_uniformity"]
        assert observed["witness"]["inputs"] == expected["witness"]["solutions"]
        self.evidence(session + "-reference", receipt)
        documents = DocumentStore(
            store_path(self.path / "instance/control"), producer="quality-calibration"
        )
        try:
            document = seed_case(
                documents,
                session,
                self.quality["cases"][session],
                {
                    "execution_id": execution,
                    "attempt_id": receipt["attempt_id"],
                    "stdout": stdout,
                },
            )
        finally:
            documents.close()
        self.evidence(
            session + "-seeded", {"document": document, "revision": 1, "execution": execution}
        )
        requested = self.post(session, "reviews", {})
        self.evidence(session + "-review-request", requested)
        self.send_once(session, session, CALIBRATION_MESSAGE)

    def hold(self):
        self.send_once(
            SESSION,
            "recovery-hold",
            (
                "Prepare the controlled recovery checkpoint now. Save the current results, plan and "
                "living document. Ask your existing computational child to message you its checkpoint "
                "and then wait_agent_message(300). Preserve its work identity. Once it is waiting, "
                "you should also wait_agent_message(300), without launching further computations, "
                "documents or model-heavy tasks until the operator sends the continuation. "
                "This is a bounded interruption test, not research completion. Forward these "
                "standing continuation instructions to the child before waiting: after the "
                "operator's single Resume gives the same work a new attempt, finish the GF(32) "
                "comparison and report to your parent instead of remaining at this hold. "
                "For you, once your own resumed attempt begins, the following standing "
                "instruction applies: " + prompt("resumed")
            ),
        )

    def interrupt(self):
        self.ensure_live()
        marker = self.path / "quality-interruption-intent.json"
        assert not marker.exists(), "inspect the original replacement/Resume; never replay it"
        assert self.inspect()["applied_child_folds"] >= 2
        # Replacement must happen at the known non-mutating mailbox wait. A race
        # is still reconciled by normal original-identity Resume, never replayed here.
        pending = self.read(
            "SELECT i.run,i.operation_id,i.tool FROM tool_intents i LEFT JOIN archive_tool_records a "
            "ON a.project=i.project AND a.session=i.session AND a.run=i.run AND a.operation_id=i.operation_id "
            "WHERE i.project=? AND a.message_id IS NULL",
            (recovery.PROJECT,),
        )
        assert len(pending) >= 2, "wait for root and child mailbox checkpoints"
        assert all(row[2] == "wait_agent_message" for row in pending), pending
        assert not self.read("SELECT session_id FROM run_leases WHERE session_id!=?", (SESSION,))
        assert not self.read(
            "SELECT request FROM research_resumes WHERE status IN ('running','queued')"
        )
        before = self.results(SESSION)
        private_json(marker, {"at": time.time(), "pending": pending, "works": before["works"]})
        self.evidence("quality-before-recovery", before)
        self.replacement(SESSION, {"pending_mailbox_waits": pending})
        self.evidence("quality-after-resume-admission", self.results(SESSION))

    def materials(self):
        self.ensure_live()
        mark = self.path / "materials-attempt.json"
        assert not mark.exists(), "inspect original file-write receipts before retrying"
        private_json(mark, {"attempted": time.time()})
        table = oracle.Field(8, 0x11B).inverse_table()
        rows = oracle.ddt(table)
        contents = {
            "quality/parameters.json": json.dumps(
                {
                    "degree": 4,
                    "modulus": "0x13",
                    "encoding": "polynomial coefficient bits",
                    "receipt_marker": self.quality["receipt_marker"],
                }
            )
            + "\n"
        }
        for index in range(8):

            def rotate(value: int, rotation: int = index) -> int:
                return ((value << rotation) | (value >> (8 - rotation))) & 255

            lines = [
                "Controlled context appendix: GF(256), modulus 0x11b; inverse followed by output bit rotation "
                + str(index)
                + ". Rows count ordered inputs; row zero is included."
            ]
            for a, row in enumerate(rows):
                changed = [0] * 256
                for b, count in enumerate(row):
                    changed[rotate(b)] = count
                lines.append(str(a) + ":" + ",".join(map(str, changed)))
            contents[f"quality/appendix-{index + 1}.txt"] = "\n".join(lines) + "\n"
        receipts = []
        for path, text in contents.items():
            found = self.worker(
                "POST",
                "/v1/files/write",
                {
                    "context": self.context(SESSION),
                    "path": path,
                    "content_b64": base64.b64encode(text.encode()).decode(),
                    "create_only": True,
                },
                operator=True,
            )
            receipts.append({"path": path, "size": found["size"], "sha256": found["sha256"]})
        self.evidence("quality-materials", receipts)

    def pressure(self, number: int):
        assert 1 <= number <= 8
        self.send_once(
            SESSION,
            f"appendix-{number}",
            f"Forward this to the existing computational child: read quality/appendix-{number}.txt completely through bounded reads. It is an explicitly controlled context-pressure table, not a new scientific claim. Check row sums, characteristic-two pairing and the affine-map interpretation. Keep scope precise and report only necessary conclusions. Retain the child for the next comparison, using wait_agent_message at its checkpoint. Do not repeat earlier appendix reads or reference calculations.",
        )

    def inspect(self) -> dict:
        works = self.read(
            "SELECT work,current,parent,state,reason,session FROM research_work "
            "WHERE project=? ORDER BY work",
            (recovery.PROJECT,),
        )
        # Read durable collections, including active and predecessor attempts.
        # A paid narrative call is not an accepted fold unless state is applied.
        folds = self.read(
            "SELECT a.work,c.run,c.position,c.item FROM run_collections c "
            "JOIN research_attempts a ON a.project=c.project AND a.session=c.session "
            "AND a.run=c.run JOIN research_work w ON w.project=a.project AND w.work=a.work "
            "WHERE c.project=? AND c.session=? AND c.collection='folds' AND w.parent IS NOT NULL",
            (recovery.PROJECT, SESSION),
        )
        accepted = [
            {"work": work, "run": run, "position": position, "fold": json.loads(item)}
            for work, run, position, item in folds
            if json.loads(item).get("state") == "applied"
        ]
        result = {
            "work": works,
            "applied_child_folds": len(accepted),
            "fold_receipts": accepted,
            "budget": self.budget(),
            "leases": self.read("SELECT project_id,session_id,run FROM run_leases"),
        }
        self.evidence("quality-inspection", result)
        return result

    def snapshot(self):
        for session in (SESSION, *CASES):
            self.evidence("quality-history-" + session, self.results(session))
        self.evidence("quality-inspection", self.inspect())
        export_records(self.path / "instance/control", self.evidence)

    def read_public_file(self, path: str, *, maximum=8 * 1024 * 1024) -> bytes:
        parts, offset, identity = [], 0, None
        while True:
            found = self.worker(
                "POST",
                "/v1/files/read",
                {
                    "context": self.context(SESSION),
                    "path": path,
                    "offset": offset,
                    "length": 1024 * 1024,
                },
                operator=True,
            )
            current = (found["size"], found["sha256"])
            assert current[0] <= maximum, "explicit export size bound exceeded"
            if identity is None:
                identity = current
            assert current == identity, "file changed during evidence capture"
            part = base64.b64decode(found["content_b64"], validate=True)
            parts.append(part)
            offset += len(part)
            if offset == identity[0]:
                break
            assert part, "file read made no progress"
        body = b"".join(parts)
        assert hashlib.sha256(body).hexdigest() == identity[1]
        return body

    def finish(self):
        """Capture terminal evidence; do not convert numeric checks to science quality."""
        assert not self.read("SELECT run FROM run_leases"), "wait for final settlement first"
        self.snapshot()
        files = {}
        paths = [
            "quality/reference.sage",
            "quality/sources/nyberg1993.pdf",
            "quality/sources/fips197.pdf",
        ]
        paths += [
            f"quality/{stem}"
            for size in (16, 32)
            for stem in (f"results-gf{size}.json", f"gf{size}.ddt.u16le")
        ]
        for path in paths:
            try:
                body = self.read_public_file(path)
            except HTTPError as error:
                if error.code != 404:
                    raise
                files[path] = {"missing": True}
                continue
            files[path] = {
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "content_b64": base64.b64encode(body).decode(),
            }
            if path.endswith(".pdf"):
                files[path]["pdf_magic"] = body.startswith(b"%PDF-")
        self.evidence("quality-public-files", files)
        grades = {}
        for field in oracle.FIELDS:
            data, binary = (
                files[f"quality/results-gf{field.order}.json"],
                files[f"quality/gf{field.order}.ddt.u16le"],
            )
            if data.get("missing") or binary.get("missing"):
                grades[str(field.order)] = {"passed": False, "reason": "required output missing"}
                continue
            try:
                grades[str(field.order)] = grade_result(
                    json.loads(base64.b64decode(data["content_b64"])),
                    base64.b64decode(binary["content_b64"]),
                    field,
                )
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
                grades[str(field.order)] = {"passed": False, "reason": type(error).__name__}
        self.evidence(
            "quality-numeric-grades",
            {
                "grades": grades,
                "scientific_quality_qualified": False,
                "required_manual_review": "literature attribution and read scope, exact archive recall, real reviewer finding then author correction and independent confirmation, preserved correct control, plan/document transitions, Resume continuity",
            },
        )
        return {
            "numeric_grades": grades,
            "scientific_rubric": "requires evidence review",
            "budget": self.budget(),
        }

    def cleanup(self):
        assert not self.read("SELECT run FROM run_leases"), "finish or explicitly abort active work"
        assert (self.path / "evidence/quality-numeric-grades.json").exists(), (
            "capture finish evidence, or use abort"
        )
        super().cleanup(incomplete=True)

    def abort(self):
        # A failed bring-up may precede creation of the control database. Do not
        # turn optional scientific reporting into a prerequisite for teardown.
        try:
            if store_path(self.path / "instance/control").exists():
                try:
                    self.snapshot()
                except Exception as error:
                    # Abort is the failure cleanup boundary. A corrupt or incomplete
                    # research store must not prevent exact owned service teardown.
                    self.evidence(
                        "quality-abort-export-error",
                        {"error_type": type(error).__name__, "scientific_quality_qualified": False},
                    )
            super().cleanup(incomplete=True)
        except Exception as error:
            self.stop_owned_services(error)
            raise RuntimeError(
                "Full cleanup is incomplete; exact owned service shutdown was checked. "
                "The private lab root is retained for VM ownership and evidence inspection."
            ) from error

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
                group = recovery.run(
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
            unit: recovery.run(
                ["systemctl", "--user", "is-active", unit], check=False
            ).stdout.strip()
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
        self.evidence("quality-abort-owned-services", receipt)
        assert inventory_error is None, (
            "process inventory incomplete; inspect preserved abort receipt"
        )
        assert not remaining, "owned processes remain; inspect preserved abort receipt"
        assert not open_ports, "owned ports remain; inspect preserved abort receipt"
        assert all(value in {"inactive", "failed", "unknown"} for value in status.values()), status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "selftest",
            "prepare",
            "authorize",
            "up",
            "guard",
            "materials",
            "calibrate",
            "initial",
            "child",
            "changed",
            "hold",
            "interrupt",
            "pressure",
            "inspect",
            "snapshot",
            "finish",
            "cleanup",
            "abort",
        ),
    )
    parser.add_argument("lab", nargs="?", type=Path)
    parser.add_argument("--static", type=Path, default=recovery.REPO / "web/workspace/dist")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--allowance", type=float)
    parser.add_argument("--authorization")
    parser.add_argument("--appendix", type=int)
    parser.add_argument("--case", choices=CASES)
    args = parser.parse_args()
    if args.action == "selftest":
        assert all(prompt(stage).strip() for stage in PROMPTS)
        # This nonexistent file proves config validation never reads the key.
        live_model(Path("/nonexistent-quality-selftest/no-key"))
        stopped = []

        def fake_stop(argv, **_):
            stopped.append(argv[-1])
            if len(stopped) == 1:
                raise TimeoutError("synthetic first-unit timeout")
            return recovery.subprocess.CompletedProcess(argv, 0)

        attempted = stop_units(["synthetic-first", "synthetic-second"], fake_stop)
        assert stopped == ["synthetic-first", "synthetic-second"]
        assert attempted["synthetic-first"]["error_type"] == "TimeoutError"
        assert attempted["synthetic-second"]["exit_code"] == 0
        print(json.dumps(asyncio.run(selftest()), indent=2))
    elif args.action == "prepare":
        print(prepare(args.static))
    else:
        if args.lab is None:
            parser.error("lab root required")
        lab = Lab(args.lab)
        if args.action == "authorize":
            if args.key_file is None or args.allowance is None or args.authorization is None:
                parser.error(
                    "authorization needs key FILE PATH, allowance and owner authorization text"
                )
            lab.authorize(args.key_file, args.allowance, args.authorization)
        elif args.action in PROMPTS:
            lab.message(args.action)
        elif args.action == "pressure":
            if args.appendix is None:
                parser.error("pressure needs --appendix 1..8")
            lab.pressure(args.appendix)
        elif args.action == "calibrate":
            if args.case is None:
                parser.error("calibrate needs --case")
            lab.calibrate(args.case)
        else:
            result = getattr(lab, args.action)()
            if result is not None:
                print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
