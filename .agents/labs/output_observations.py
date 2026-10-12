# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Marked Lab24 terminal-observation acceptance; root owns services and teardown.

Start the disposable lab with PROJECT=lab24, then run init prepare verify checkpoint
running. Root restarts only its transient worker, then runs resume restart_verify.
This helper starts/stops no service and opens only its marked lab SQLite read-only.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import shlex
import sqlite3
import time
import tomllib
from contextlib import closing
from pathlib import Path
from typing import Any

import aiohttp

from sandbox_science.contracts.output_observations import (
    MAX_MARKER_BYTES,
    OutputBinding,
    OutputScope,
)
from sandbox_science.worker.output_observations import ObservationJournal

LAB = Path.home() / ".local/state/openshell-lab"
PROJECT, ENVIRONMENT, SESSION = "lab24", "lab24-observations", "observations"
URL = "http://127.0.0.1:8100"
MARKER = LAB / "output-observations.created"
EVIDENCE = LAB / "evidence/output-observations.json"
DATABASE = LAB / "worker/state/worker.sqlite3"
STAMP = str(time.time_ns())
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "lost"}
SCOPE = OutputScope(project_id=PROJECT, session_id=SESSION, environment_id=ENVIRONMENT)


def marked(*, create: bool = False) -> None:
    """Refuse ordinary worker/project state before any API or internal journal read."""
    settings = tomllib.loads((LAB / "worker/worker.toml").read_text())
    assert settings.get("state_dir") == "state"
    assert any(client["projects"] == [PROJECT] for client in settings["clients"])
    assert settings["openshell"]["environments"]["policy_store"] == str(LAB / "middleware/state")
    expected = {
        "project": PROJECT,
        "environment": ENVIRONMENT,
        "purpose": "disposable terminal output-observation acceptance",
    }
    if create:
        with MARKER.open("x") as handle:
            json.dump(expected, handle)
    assert json.loads(MARKER.read_text()) == expected


def journal(*, target: int | None = None, stream_id: str | None = None) -> dict[str, Any]:
    """Use the internal reader on one read-only consistent snapshot; no route is added."""
    marked()
    with closing(sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        reader = object.__new__(ObservationJournal)
        reader.db = db
        first = reader.page(SCOPE, target=target, stream_id=stream_id)
        result = first.model_dump(mode="json")
        records = result["records"]
        last = first.next_after
        while last < first.target:
            page = reader.page(SCOPE, after=last, target=first.target, stream_id=first.stream_id)
            assert page.next_after > last
            records.extend(record.model_dump(mode="json") for record in page.records)
            last = page.next_after
        result["next_after"] = last
        # Same logical session/environment in another project must not reveal records.
        other = reader.page(SCOPE.model_copy(update={"project_id": PROJECT + "b"}))
        assert other.target == 0
        assert not other.records
        assert other.stream_id != first.stream_id
        return result


class Checks:
    def __init__(self, http: aiohttp.ClientSession) -> None:
        self.http = http
        self.evidence = json.loads(EVIDENCE.read_text()) if EVIDENCE.exists() else {}

    def save(self, name: str, value: object) -> None:
        self.evidence[name] = value
        EVIDENCE.parent.mkdir(exist_ok=True)
        EVIDENCE.write_text(json.dumps(self.evidence, indent=2) + "\n")
        print(f"PASS {name}", flush=True)

    async def api(
        self, method: str, path: str, body: object = None, *, status: int = 200
    ) -> dict[str, Any]:
        marked()
        async with self.http.request(method, URL + path, json=body) as response:
            raw = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                assert len(raw) + len(chunk) <= 1 << 20
                raw.extend(chunk)
            value = json.loads(raw)
            assert response.status == status, (path, response.status, value)
            return value

    async def submit(self, name: str, *, kind: str = "shell", **fields: object) -> dict[str, Any]:
        return await self.api(
            "POST",
            "/v1/executions",
            {
                "operation_id": f"lab24-{name}-{STAMP}",
                "kind": kind,
                "context": SCOPE.model_dump(),
                "timeout_seconds": 30,
                **fields,
            },
            status=201,
        )

    async def ended(self, value: dict[str, Any], expected: str) -> dict[str, Any]:
        async with asyncio.timeout(60):
            while value["state"] not in TERMINAL:
                await asyncio.sleep(0.1)
                value = await self.api("GET", f"/v1/executions/{value['execution_id']}")
        assert value["state"] == expected, value
        return value

    async def shell(
        self, name: str, script: str, expected: str = "succeeded", **fields: object
    ) -> dict[str, Any]:
        return await self.ended(await self.submit(name, script=script, **fields), expected)

    async def wait_file(self, path: str) -> None:
        async with asyncio.timeout(30):
            while True:
                marked()
                async with self.http.post(
                    URL + "/v1/files/read",
                    json={
                        "context": SCOPE.model_dump(),
                        "path": path,
                        "length": 1024,
                    },
                ) as response:
                    if response.status == 200:
                        return
                    value = await response.json()
                    assert response.status == 404, value
                await asyncio.sleep(0.1)

    async def prepare(self) -> None:
        environment = await self.api(
            "POST",
            "/v1/environments",
            {
                "project_id": PROJECT,
                "environment_id": ENVIRONMENT,
                "profile": "minimal",
                "policy": (LAB / "policy.digest").read_text().strip(),
            },
            status=201,
        )
        self.save("environment", environment)

    async def verify(self) -> None:
        code = """import os
for name in (b'\\xfe', b'\\xff', '\ufffd'.encode()):
    fd=os.open(name, os.O_CREAT|os.O_WRONLY, 0o600); os.write(fd,b'raw'); os.close(fd)
os.makedirs('shared', exist_ok=True)
open('shared/collision','wb').write(b'session')
open(os.environ['PROJECT_SHARED']+'/collision','wb').write(b'shared')
open('removed','wb').write(b'present')
os.symlink('/etc/passwd','link')
os.mkfifo('fifo')
"""
        outcomes = [await self.shell("raw", "python -I -S -c " + shlex.quote(code))]
        outcomes.append(await self.shell("failed", "printf partial > failed; exit 7", "failed"))
        for name, code, expected in (
            ("cell", "lab24_value=1729; open('cell','w').write('known')", "succeeded"),
            (
                "cell-failed",
                "open('cell-failed','w').write('partial'); raise RuntimeError('expected')",
                "failed",
            ),
        ):
            outcomes.append(
                await self.ended(
                    await self.submit(name, kind="cell", language="python", code=code), expected
                )
            )
        outcomes.append(
            await self.ended(
                await self.submit(
                    "job",
                    kind="job",
                    script="printf job > job",
                    purpose="observation fixture",
                    expected_progress="terminal",
                ),
                "succeeded",
            )
        )
        outcomes.append(
            await self.ended(
                await self.submit(
                    "job-failed",
                    kind="job",
                    script="printf partial > job-failed; exit 7",
                    purpose="failed partial fixture",
                    expected_progress="failed terminal",
                ),
                "failed",
            )
        )
        capabilities = await self.api("GET", f"/v1/capabilities?environment_id={ENVIRONMENT}")
        pdf_execution = None
        if "pdf" in capabilities["features"]:
            # Fixed synthetic PDF fixture; trusted host never parses/renders its content.
            stream = b"BT /F1 12 Tf 20 40 Td (Lab24) Tj ET"
            objects = [
                b"<< /Type /Catalog /Pages 2 0 R >>",
                b"<< /Type /Pages /Kids [5 0 R] /Count 1 >>",
                b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>",
            ]
            data, offsets = bytearray(b"%PDF-1.4\n"), [0]
            for number, body in enumerate(objects, 1):
                offsets.append(len(data))
                data.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
            xref = len(data)
            data.extend(b"xref\n0 6\n0000000000 65535 f \n")
            for offset in offsets[1:]:
                data.extend(f"{offset:010} 00000 n \n".encode())
            data.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
            encoded = base64.b64encode(data).decode()
            outcomes.append(
                await self.shell(
                    "pdf-source",
                    "python -I -S -c "
                    + shlex.quote(
                        f"import base64; open('source.pdf','wb').write(base64.b64decode('{encoded}'))"
                    ),
                )
            )
            pdf_execution = await self.ended(
                await self.submit(
                    "pdf",
                    kind="pdf",
                    operation="render",
                    path="source.pdf",
                    page=1,
                    out="observation.png",
                    dpi=36,
                ),
                "succeeded",
            )
            outcomes.append(pdf_execution)
        self.save(
            "pdf-boundary",
            {
                "capabilities": capabilities,
                "tested": pdf_execution is not None,
                "reason": "actual PDF render" if pdf_execution else "PDF capability unavailable",
            },
        )
        for kind in ("shell", "cell", "job"):
            name = f"cancel-{kind}"
            fields: dict[str, object] = {"script": f"printf partial > {name}; sleep 30"}
            if kind == "cell":
                fields = {
                    "language": "python",
                    "code": f"import time; open('{name}','w').write('partial'); time.sleep(30)",
                }
            if kind == "job":
                fields.update(purpose="partial fixture", expected_progress="cancellation")
            value = await self.submit(name, kind=kind, **fields)
            await self.wait_file(name)
            await self.api("POST", f"/v1/executions/{value['execution_id']}/cancel")
            outcomes.append(await self.ended(value, "cancelled"))
        continuity = await self.ended(
            await self.submit(
                "kernel-continuity",
                kind="cell",
                language="python",
                code="assert lab24_value==1729; print('kernel retained 1729')",
            ),
            "succeeded",
        )
        assert continuity["result"]["stdout"]["text"] == "kernel retained 1729\n"
        outcomes.append(continuity)
        outcomes.append(
            await self.shell(
                "timeout", "printf partial > timeout; sleep 30", "timed_out", timeout_seconds=1
            )
        )
        outcomes.append(await self.shell("absent", "rm removed"))
        outcomes.append(await self.shell("refused", "true", "failed", cwd="absent-directory"))
        outcomes.append(
            await self.shell(
                "truncated",
                "python -I -S -c "
                + shlex.quote(
                    "import pathlib; [pathlib.Path('many-%03d'%n).touch() for n in range(300)]"
                ),
            )
        )
        outcomes.append(await self.shell("healthy", "printf 'healthy after observations\\n'"))
        page = journal()
        self.verify_reports(outcomes, page, pdf_execution)
        self.save("terminal-outcomes", outcomes)
        self.save("scoped-journal", page)

    def verify_reports(
        self,
        outcomes: list[dict[str, Any]],
        page: dict[str, Any],
        pdf_execution: dict[str, Any] | None = None,
    ) -> None:
        """Require every exercised outcome's source report and known partial path."""
        by_id = {record["marker"]["binding"]["execution_id"]: record for record in page["records"]}
        environment = self.evidence["environment"]
        for outcome in outcomes:
            record = by_id[outcome["execution_id"]]
            marker, pin = record["marker"], record["marker"]["binding"]
            assert marker["state"] == outcome["state"]
            assert pin["attempt_id"] == outcome["attempt_id"]
            assert pin["sandbox"] == environment["sandbox"]
            assert pin["sandbox_id"] == environment["sandbox_id"]
            assert pin["producer"] == pin["observed_inputs"] == "unknown"
            assert marker["potentially_partial"] == (outcome["state"] != "succeeded")
            if outcome["operation_id"] != f"lab24-refused-{STAMP}":
                assert record["report"] is not None
                assert record["report"]["coverage"]["mutation_coverage"] == "incomplete"
        by_operation = {
            outcome["operation_id"]: by_id[outcome["execution_id"]] for outcome in outcomes
        }
        expected_paths = {
            "failed": b"failed",
            "cell": b"cell",
            "cell-failed": b"cell-failed",
            "job": b"job",
            "job-failed": b"job-failed",
            "cancel-shell": b"cancel-shell",
            "cancel-cell": b"cancel-cell",
            "cancel-job": b"cancel-job",
            "timeout": b"timeout",
        }
        for name, path in expected_paths.items():
            record = by_operation[f"lab24-{name}-{STAMP}"]
            matches = [
                entry
                for entry in record["report"]["entries"]
                if entry["area"] == "session" and base64.b64decode(entry["path_b64"]) == path
            ]
            assert len(matches) == 1, (name, path, matches)
            entry = matches[0]
            assert entry["after"] is not None
            assert entry["after"]["size"] > 0
            assert type(entry["after"]["mtime_ns"]) is int
            assert type(entry["after"]["inode"]) is int
        raw = by_id[outcomes[0]["execution_id"]]["report"]
        keys = {(entry["area"], base64.b64decode(entry["path_b64"])) for entry in raw["entries"]}
        assert keys >= {
            ("session", b"\xfe"),
            ("session", b"\xff"),
            ("session", "\ufffd".encode()),
            ("session", b"shared/collision"),
            ("shared", b"collision"),
        }
        assert ("session", b"link") not in keys
        assert ("session", b"fifo") not in keys
        absent = by_operation[f"lab24-absent-{STAMP}"]["report"]
        assert any(
            entry["kind"] == "not_seen_after" and base64.b64decode(entry["path_b64"]) == b"removed"
            for entry in absent["entries"]
        )
        refused = by_operation[f"lab24-refused-{STAMP}"]
        assert refused["marker"]["gap"] == "missing"
        assert refused["report"] is None
        truncated = by_operation[f"lab24-truncated-{STAMP}"]["report"]
        assert truncated["coverage"]["report_truncated"]
        assert truncated["coverage"]["after_limited"]
        if pdf_execution is not None:
            assert any(
                base64.b64decode(entry["path_b64"]) == b"observation.png"
                for entry in by_id[pdf_execution["execution_id"]]["report"]["entries"]
            )

    async def checkpoint(self) -> None:
        page = journal()
        self.save(
            "checkpoint",
            {
                "target": page["target"],
                "stream_id": page["stream_id"],
                "sha256": hashlib.sha256(json.dumps(page, sort_keys=True).encode()).hexdigest(),
            },
        )

    async def running(self) -> None:
        value = await self.submit(
            "running",
            kind="job",
            script="printf before >> runs; while [ ! -f release ]; do sleep .1; done; printf after >> runs",
            purpose="durable original-attempt fixture",
            expected_progress="root worker restart and release",
            timeout_seconds=300,
        )
        await self.wait_file("runs")
        marked()
        with closing(sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True)) as db:
            raw = db.execute(
                "SELECT CASE WHEN typeof(binding)='blob' THEN substr(binding,1,?) END"
                " FROM output_observation_attempts WHERE execution=? AND project=? AND session=?"
                " AND environment=?",
                (MAX_MARKER_BYTES + 1, value["execution_id"], PROJECT, SESSION, ENVIRONMENT),
            ).fetchone()[0]
        pin = OutputBinding.model_validate_json(raw)
        assert pin.dispatched
        assert pin.incarnation is not None
        assert pin.sandbox_id == self.evidence["environment"]["sandbox_id"]
        current = await self.api("GET", f"/v1/executions/{value['execution_id']}")
        assert current["state"] not in TERMINAL
        self.save("running", {"execution": current, "binding": pin.model_dump(mode="json")})

    async def resume(self) -> None:
        value = self.evidence["running"]["execution"]
        continuity = await self.ended(
            await self.submit(
                "restarted-kernel",
                kind="cell",
                language="python",
                code="assert lab24_value==1729; print('kernel retained after restart')",
            ),
            "succeeded",
        )
        assert continuity["result"]["stdout"]["text"] == "kernel retained after restart\n"
        await self.shell("release", "touch release")
        ended = await self.ended(value, "succeeded")
        read = await self.api(
            "POST",
            "/v1/files/read",
            {"context": SCOPE.model_dump(), "path": "runs", "length": 1024},
        )
        assert base64.b64decode(read["content_b64"]) == b"beforeafter"
        assert ended["attempt_id"] == value["attempt_id"]
        record = next(
            item
            for item in journal()["records"]
            if item["marker"]["binding"]["execution_id"] == value["execution_id"]
        )
        assert (
            record["marker"]["binding"]["sandbox_id"] == self.evidence["environment"]["sandbox_id"]
        )
        assert record["marker"]["binding"] == self.evidence["running"]["binding"]
        self.save(
            "resumed",
            {"execution": ended, "record": record, "read": read, "kernel-continuity": continuity},
        )

    async def restart_verify(self) -> None:
        checkpoint = self.evidence["checkpoint"]
        page = journal(target=checkpoint["target"], stream_id=checkpoint["stream_id"])
        assert (
            hashlib.sha256(json.dumps(page, sort_keys=True).encode()).hexdigest()
            == checkpoint["sha256"]
        )
        self.save("durable-prefix", checkpoint)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "parts",
        nargs="+",
        choices=("init", "prepare", "verify", "checkpoint", "running", "resume", "restart_verify"),
    )
    args = parser.parse_args()
    if "init" in args.parts:
        assert args.parts[0] == "init"
        marked(create=True)
    else:
        marked()
    token = (LAB / "worker/client-a.token").read_text().strip()
    async with aiohttp.ClientSession(
        headers={"Authorization": f"Bearer {token}"}, timeout=aiohttp.ClientTimeout(total=90)
    ) as http:
        checks = Checks(http)
        for part in args.parts:
            if part != "init":
                await getattr(checks, part)()


if __name__ == "__main__":
    asyncio.run(main())
