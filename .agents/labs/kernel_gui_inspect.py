# SPDX-License-Identifier: MIT
# ruff: noqa: S608 -- SQL fragments contain only fixed source column names and limits.
"""SQL-only Lab27 checkpoints and passive gateway-stopped acceptance monitor. Run only by the root lab operator.

The phase is an operator label, not live-connection proof. Inspection creates no
store or service, performs no SQL writes, and never selects private daemon tokens.
Separate database transactions are independent observations, not an atomic snapshot.
The monitor projects only immutable sandbox IDs, names and phase from gateway CLI
reads; raw CLI output remains in memory and is never printed or archived.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from kernel_gui import EVIDENCE, bounded, guard

from sandbox_science.contracts import EnvironmentInfo, Execution, ExecutionState
from sandbox_science.orchestrator.projects import ProjectRecord
from sandbox_science.worker.kernel_revisions import KernelBirth

LAB = Path.home() / ".local/state/openshell-lab"
PROJECT = "lab27"
SOURCE_HEAD = ""
TERMINAL = {
    ExecutionState.SUCCEEDED,
    ExecutionState.FAILED,
    ExecutionState.CANCELLED,
    ExecutionState.TIMED_OUT,
    ExecutionState.LOST,
}


class RefusedError(Exception):
    """Fixed errors only: input records must never appear in diagnostics."""


def require(condition: bool) -> None:
    if not condition:
        raise RefusedError("invalid or out-of-bound marked lab metadata")


def owned(path: Path, *, directory: bool = False) -> os.stat_result:
    require(path.is_relative_to(Path.home()))
    current = Path.home()
    info = current.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid())
    for component in path.relative_to(current).parts:
        current /= component
        info = current.lstat()
        require(info.st_uid == os.getuid() and not stat.S_ISLNK(info.st_mode))
        require(
            stat.S_ISDIR(info.st_mode)
            if current != path or directory
            else stat.S_ISREG(info.st_mode)
        )
    return info


def marked() -> None:
    guard(SOURCE_HEAD)
    owned(LAB, directory=True)


def text(value: object, maximum: int = 128, *, pattern: str | None = None) -> str:
    require(isinstance(value, bytes) and 0 < len(value) <= maximum)
    result = value.decode("utf-8", "strict")
    require(re.fullmatch(pattern or r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", result) is not None)
    return result


def integer(value: object, *, maximum: int = 2**62) -> int:
    require(type(value) is int and 0 <= value <= maximum)
    return value


def clipped(column: str, maximum: int) -> str:
    # All callers supply fixed source constants, never input SQL identifiers.
    return f"substr(CAST({column} AS BLOB),1,{maximum + 1})"


def number(column: str) -> str:
    return f"CASE WHEN typeof({column})='integer' THEN {column} END"


def record(column: str, maximum: int) -> str:
    return (
        f"CASE WHEN typeof({column})='text' AND length(CAST({column} AS BLOB))<={maximum}"
        f" THEN CAST({column} AS BLOB) END"
    )


@contextlib.contextmanager
def database(name: str):
    marked()
    path = LAB / "worker/state" / name
    before = owned(path)
    require(before.st_size <= 64 * 1024 * 1024)
    with contextlib.closing(
        sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
    ) as db:
        after = owned(path)
        require((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino))
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 256 * 1024)
        deadline = time.monotonic() + 2
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        db.execute("BEGIN")
        try:
            yield db
        finally:
            db.rollback()  # Release a read transaction only; no SQL writes occurred.
    marked()


def environment(environment_id: str) -> dict[str, object] | None:
    with database("environments.sqlite3") as db:
        row = db.execute(
            f"SELECT {record('document', 8192)} FROM environments WHERE environment_id=? LIMIT 2",
            (environment_id,),
        ).fetchall()
    require(len(row) <= 1)
    if not row:
        return None
    require(isinstance(row[0][0], bytes))
    info = EnvironmentInfo.model_validate_json(row[0][0])
    require((info.project_id, info.environment_id) == (PROJECT, environment_id))
    require(re.fullmatch(r"ss-[0-9a-f]{16}", info.sandbox) is not None)
    if info.sandbox_id is not None:
        require(re.fullmatch(r"[0-9a-f-]{36}", info.sandbox_id) is not None)
    return {
        "project_id": PROJECT,
        "environment_id": environment_id,
        "state": info.state.value,
        "sandbox": info.sandbox,
        "sandbox_id": info.sandbox_id,
    }


def worker(environment_id: str) -> dict[str, object]:
    with database("worker.sqlite3") as db:
        # Explicit allowlist: no token, endpoint, request/code, output or settings columns.
        endpoints = db.execute(
            f"SELECT {clipped('incarnation', 28)},{clipped('sandbox', 128)},"
            f"{clipped('sandbox_id', 128)} FROM executor_endpoints WHERE environment_id=? LIMIT 2",
            (environment_id,),
        ).fetchall()
        require(len(endpoints) <= 1)
        endpoint = None
        if endpoints:
            inc, sandbox, sandbox_id = endpoints[0]
            endpoint = {
                "incarnation": text(inc, 28, pattern=r"inc-[0-9a-f]{24}"),
                "sandbox": text(sandbox, pattern=r"ss-[0-9a-f]{16}"),
                "sandbox_id": text(sandbox_id, 36, pattern=r"[0-9a-f-]{36}"),
            }
        generations = db.execute(
            f"SELECT {clipped('session_id', 128)},{clipped('language', 6)},"
            f"{clipped('incarnation', 28)},{number('base')},{number('last')}"
            " FROM kernels WHERE environment_id=? ORDER BY session_id,language LIMIT 7",
            (environment_id,),
        ).fetchall()
        require(len(generations) <= 6)
        generation_rows = []
        for session, language, incarnation, base, last in generations:
            require(integer(base) < integer(last) and text(session) in {"a", "b", "c", "empty"})
            generation_rows.append(
                {
                    "session_id": text(session),
                    "language": text(language, 6, pattern=r"python|sage"),
                    "incarnation": text(incarnation, 28, pattern=r"inc-[0-9a-f]{24}"),
                    "base": base,
                    "last": last,
                }
            )
        births = db.execute(
            f"SELECT {clipped('session_id', 128)},{clipped('language', 6)},{record('record', 2048)}"
            " FROM kernel_start_revisions WHERE environment_id=?"
            " ORDER BY session_id,language LIMIT 7",
            (environment_id,),
        ).fetchall()
        require(len(births) <= 6)
        birth_rows = []
        for session, language, raw in births:
            require(isinstance(raw, bytes))
            pin = KernelBirth.model_validate_json(raw)
            require(pin.session_id in {"a", "b", "c", "empty"})
            require(pin.kernel_id == f"krn-{pin.session_id}-{pin.language}")
            if pin.sandbox is not None:
                require(re.fullmatch(r"ss-[0-9a-f]{16}", pin.sandbox) is not None)
                require(
                    pin.sandbox_id is not None
                    and re.fullmatch(r"[0-9a-f-]{36}", pin.sandbox_id) is not None
                )
            require(
                (pin.project_id, pin.environment_id, pin.session_id, pin.language)
                == (
                    PROJECT,
                    environment_id,
                    text(session),
                    text(language, 6, pattern=r"python|sage"),
                )
            )
            birth_rows.append(pin.model_dump(mode="json"))
        executions = db.execute(
            f"SELECT {clipped('execution_id', 128)},{clipped('session_id', 128)},"
            f"{number('terminal')},{record('record', 65536)} FROM executions"
            " WHERE environment_id=? ORDER BY execution_id LIMIT 33",
            (environment_id,),
        ).fetchall()
        require(len(executions) <= 32)
        counts: Counter[str] = Counter()
        execution_rows = []
        for execution_id, session, terminal, raw in executions:
            require(isinstance(raw, bytes))
            execution = Execution.model_validate_json(raw)
            require(execution.context.session_id in {"a", "b", "c", "empty"})
            require(re.fullmatch(r"exe-[0-9a-f]{24}", execution.execution_id) is not None)
            require(re.fullmatch(r"att-[0-9a-f]{24}", execution.attempt_id) is not None)
            require(
                (
                    execution.execution_id,
                    execution.context.project_id,
                    execution.context.environment_id,
                    execution.context.session_id,
                )
                == (text(execution_id), PROJECT, environment_id, text(session))
            )
            require(integer(terminal, maximum=1) == int(execution.state in TERMINAL))
            require((execution.state not in TERMINAL) or execution.result is not None)
            counts[f"state_{execution.state.value}"] += 1
            counts["typed_records"] += 1
            counts["terminal_rows"] += terminal
            counts["cell_start_pins"] += int(
                execution.kind == "cell" and execution.kernel is not None
            )
            counts["result_kernel_pins"] += int(
                execution.result is not None and execution.result.kernel is not None
            )
            execution_rows.append(
                {
                    "execution_id": execution.execution_id,
                    "attempt_id": execution.attempt_id,
                    "session_id": execution.context.session_id,
                    "kind": execution.kind,
                    "state": execution.state.value,
                }
            )
    return {
        "remembered_endpoint": endpoint,
        "generation_rows": generation_rows,
        "birth_rows": birth_rows,
        "execution_coherence_counts": dict(sorted(counts.items())),
        "execution_identities": execution_rows,
    }


def snapshot(phase: str) -> dict[str, object]:
    marked()
    values = json.loads(bounded(EVIDENCE, 1024 * 1024))
    project = ProjectRecord.model_validate_json(json.dumps(values["registry"]))
    require(project.project_id == PROJECT)
    require(project.sessions["a"].environment_id == project.sessions["b"].environment_id)
    identities = [project.sessions[s].environment_id for s in ("a", "c")]
    require(len(set(identities)) == 2)
    return {
        "schema": 1,
        "source_head": SOURCE_HEAD,
        "phase_label": phase,
        "recorded_at": datetime.now(UTC).isoformat(),
        "project_id": PROJECT,
        "environments": [
            {"environment": environment(identity), "worker": worker(identity)}
            for identity in identities
        ],
        "live_connection_proven": False,
    }


def write(path: Path, value: object) -> None:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    require(len(encoded) <= 32768)
    with path.open("xb") as stream:
        stream.write(encoded + b"\n")


def monitor() -> None:
    marked()
    before = None
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        values = json.loads(bounded(EVIDENCE, 1024 * 1024))
        if before is None and "idle-suspended-during-GUI-observation" in values:
            before = snapshot("stopped-before-thirty-reads")
            require(all(v["environment"]["state"] == "stopped" for v in before["environments"]))
            write(LAB / "evidence/kernel-gui-stopped-before.json", before)
        if before is not None and "thirty-stopped-GUI-observations" in values:
            after = snapshot("stopped-after-thirty-reads")
            require(before["environments"] == after["environments"])
            write(LAB / "evidence/kernel-gui-stopped-after.json", after)
            cli_env = {
                **os.environ,
                "XDG_CONFIG_HOME": str(LAB / "config"),
                "XDG_STATE_HOME": str(LAB / "state"),
                "XDG_DATA_HOME": str(LAB / "data"),
                "OPENSHELL_LOCAL_TLS_DIR": str(LAB / "state/openshell/tls"),
                "OPENSHELL_TELEMETRY_ENABLED": "false",
            }
            phases = []
            for entry in after["environments"]:
                expected = entry["environment"]
                stop_deadline = time.monotonic() + 5
                while time.monotonic() < stop_deadline:
                    raw = subprocess.run(  # noqa: S603 -- pinned CLI, validated marked-lab sandbox
                        [
                            str(Path.home() / ".local/opt/openshell-0.1.5/bin/openshell"),
                            "sandbox",
                            "get",
                            expected["sandbox"],
                            "-o",
                            "json",
                        ],
                        env=cli_env,
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=5,
                    )
                    require(len(raw.stdout.encode()) <= 1024 * 1024)
                    sandbox = json.loads(raw.stdout)
                    require(sandbox["id"] == expected["sandbox_id"])
                    require(sandbox["name"] == expected["sandbox"])
                    require(isinstance(sandbox["phase"], str) and len(sandbox["phase"]) <= 32)
                    if sandbox["phase"].lower() == "stopped":
                        phases.append({k: sandbox[k] for k in ("id", "name", "phase")})
                        break
                    time.sleep(0.05)
                else:
                    raise RefusedError("same sandbox was not observed stopped")
            write(
                LAB / "evidence/kernel-gui-gateway-stopped.json",
                {
                    "source_head": SOURCE_HEAD,
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "sandboxes": phases,
                    "sql_pins_unchanged_over_thirty_reads": True,
                },
            )
            print(
                "PASS actual gateway stopped and SQL pins unchanged over thirty GUI reads",
                flush=True,
            )
            return
        time.sleep(0.05)
    raise RefusedError("no complete stopped checkpoint appeared")


def main() -> None:
    global SOURCE_HEAD
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("inspect", "monitor"))
    parser.add_argument("--head", required=True)
    parser.add_argument("--phase", default="operator-checkpoint")
    args = parser.parse_args()
    SOURCE_HEAD = args.head
    require(re.fullmatch(r"[a-z-]{1,64}", args.phase) is not None)
    if args.mode == "monitor":
        monitor()
    else:
        value = snapshot(args.phase)
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        require(len(encoded) <= 32768)
        print(encoded.decode())


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never echo exceptions that may quote a damaged record/private input.
        print(
            json.dumps(
                {"ok": False, "error_class": type(error).__name__, "error": "inspection refused"}
            )
        )
        raise SystemExit(1) from None
