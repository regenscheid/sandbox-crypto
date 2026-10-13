# SPDX-License-Identifier: MIT
# ruff: noqa: S101, S607
"""Root-owned disposable Lab28 passive-lineage acceptance; no service lifecycle.

Run init/before from reviewed clean --head with PROJECT=lab28 and no idle timer.
Root gracefully restarts its worker, then runs restarted. Root enables the owned
worker's suspend_after=3 and restarts it before idle. After idle, root retains the
actual gateway `sandbox get <saved name> -o json` output and writes its safe
{name,id,phase} projection plus the marker's {run_id,source_head,helper_sha256}
to evidence/lineage-gateway-stopped.json, then runs resume. That fresh, run-bound
gateway identity/Stopped proof is required; API stopped alone is insufficient.
Root owns source/settings/services/VMs and full teardown. Unknown/legacy proof is
not manufactured by corrupting records or bypassing normal execution admission.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import time
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp

from sandbox_science.contracts import (
    ExecutionContext,
    KernelBirthPin,
    KernelLineageIdentity,
    KernelLineageObservation,
    kernel_lineage_sha256,
)
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

SOURCE = Path(__file__).resolve().parents[2]
LAB = Path.home() / ".local/state/openshell-lab"
PROJECT = "lab28"
ENVIRONMENT = "lab28-kernel-lineage"
URL = "http://127.0.0.1:8100"
MARKER = "kernel-lineage.created"
EVIDENCE = "evidence/kernel-lineage.json"
GATEWAY = "evidence/lineage-gateway-stopped.json"
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "lost"}
STAMP = str(time.time_ns())


def directory(relative: str = "") -> int:
    """Open only owned, non-symlink lab directories; caller owns the descriptor."""
    held = os.open(LAB, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        assert os.fstat(held).st_uid == os.getuid()
        for name in relative.split("/") if relative else ():
            assert name not in {"", ".", ".."}
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=held)
            os.close(held)
            held = child
            assert os.fstat(held).st_uid == os.getuid()
        return held
    except BaseException:
        os.close(held)
        raise


def bounded_file(relative: str, limit: int = 65536) -> tuple[bytes, os.stat_result]:
    """Read bounded bytes and metadata from the same owned regular descriptor."""
    parent, _, name = relative.rpartition("/")
    held = directory(parent)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=held)
        try:
            info = os.fstat(fd)
            assert stat.S_ISREG(info.st_mode)
            assert info.st_uid == os.getuid()
            assert info.st_size <= limit
            raw = os.read(fd, limit + 1)
        finally:
            os.close(fd)
    finally:
        os.close(held)
    assert len(raw) <= limit
    return raw, info


def bounded(relative: str, limit: int = 65536) -> bytes:
    return bounded_file(relative, limit)[0]


def write_new(relative: str, raw: bytes) -> None:
    parent, _, name = relative.rpartition("/")
    held = directory(parent)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=held)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
    finally:
        os.close(held)


def guard(head: str, *, create: bool = False) -> dict[str, Any]:
    assert re.fullmatch(r"[0-9a-f]{40}", head)
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip() == head
    )
    assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=SOURCE, text=True)
    settings = tomllib.loads(bounded("worker/worker.toml").decode())
    assert settings["listen"] == "127.0.0.1:8100"
    assert any(client["projects"] == [PROJECT] for client in settings["clients"])
    assert settings["openshell"].get("daemon", True) is True
    assert settings["openshell"]["environments"]["profiles"]["minimal"].startswith(
        "sandbox-science/minimal:"
    )
    expected = {
        "version": 1,
        "project": PROJECT,
        "purpose": "disposable passive kernel lineage acceptance",
        "source_head": head,
        "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    if create:
        write_new(
            MARKER, json.dumps({**expected, "run_id": uuid.uuid4().hex}, sort_keys=True).encode()
        )
    marker = json.loads(bounded(MARKER, 2048))
    run_id = marker.pop("run_id")
    assert re.fullmatch(r"[0-9a-f]{32}", run_id)
    assert marker == expected
    return settings


def snapshot(head: str) -> dict[str, Any]:
    """One bounded read transaction; exclude daemon endpoint secrets and tokens."""
    guard(head)
    # Open the verified file through its held descriptor to avoid a path swap.
    held = directory("worker/state")
    try:
        fd = os.open("worker.sqlite3", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=held)
    finally:
        os.close(held)
    try:
        info = os.fstat(fd)
        assert stat.S_ISREG(info.st_mode)
        assert info.st_uid == os.getuid()
        with sqlite3.connect(f"file:/proc/self/fd/{fd}?mode=ro", uri=True) as db:
            db.execute("BEGIN")
            endpoint = db.execute(
                "SELECT substr(CAST(incarnation AS BLOB),1,29),substr(CAST(sandbox AS BLOB),1,129),"
                "substr(CAST(sandbox_id AS BLOB),1,129) FROM executor_endpoints WHERE environment_id=?",
                (ENVIRONMENT,),
            ).fetchone()
            if endpoint is not None:
                endpoint = [value.decode("ascii") for value in endpoint]
                assert re.fullmatch(r"inc-[0-9a-f]{24}", endpoint[0])
                assert all(
                    re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value)
                    for value in endpoint[1:]
                )
            rows = db.execute(
                "SELECT substr(CAST(record AS BLOB),1,2049) FROM kernel_start_revisions"
                " WHERE environment_id=? ORDER BY session_id,language LIMIT 7",
                (ENVIRONMENT,),
            ).fetchall()
            assert len(rows) <= 6
            births = []
            for (raw,) in rows:
                assert isinstance(raw, bytes)
                assert len(raw) <= 2048
                birth = KernelBirthPin.model_validate_json(raw, strict=True)
                assert birth.project_id == PROJECT
                assert birth.environment_id == ENVIRONMENT
                assert birth.session_id in {"a", "b", "empty"}
                births.append(birth.model_dump(mode="json"))
            rows = db.execute(
                "SELECT substr(CAST(session_id AS BLOB),1,129),substr(CAST(language AS BLOB),1,7),"
                "substr(CAST(incarnation AS BLOB),1,29),"
                "CASE WHEN typeof(base)='integer' THEN base END,"
                "CASE WHEN typeof(last)='integer' THEN last END"
                " FROM kernels WHERE environment_id=? ORDER BY session_id,language LIMIT 7",
                (ENVIRONMENT,),
            ).fetchall()
            assert len(rows) <= 6
            generations = []
            for session, language, incarnation, base, last in rows:
                session, language, incarnation = (
                    v.decode("ascii") for v in (session, language, incarnation)
                )
                assert session in {"a", "b", "empty"}
                assert language in {"python", "sage"}
                assert re.fullmatch(r"inc-[0-9a-f]{24}", incarnation)
                assert type(base) is int
                assert type(last) is int
                assert 0 <= base <= last <= 2**62
                generations.append([session, language, incarnation, base, last])
    finally:
        os.close(fd)
    value = {"endpoint": endpoint, "births": births, "generations": generations}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return {"rows": value, "sha256": hashlib.sha256(raw).hexdigest()}


class Checks:
    def __init__(self, head: str, http: aiohttp.ClientSession, worker: WorkerClient) -> None:
        guard(head)
        self.head, self.http, self.worker = head, http, worker
        try:
            self.values = json.loads(bounded(EVIDENCE, 2 * 1024 * 1024))
        except FileNotFoundError:
            self.values = {}

    def save(self, key: str, value: Any) -> None:
        guard(self.head)
        assert key not in self.values, "Use fresh marked state; no implicit stage replay"
        self.values[key] = value
        raw = (json.dumps(self.values, indent=2) + "\n").encode()
        assert len(raw) <= 2 * 1024 * 1024
        held = directory()
        try:
            with contextlib.suppress(FileExistsError):
                os.mkdir("evidence", 0o700, dir_fd=held)
        finally:
            os.close(held)
        write_new(EVIDENCE + ".partial", raw)
        held = directory("evidence")
        try:
            os.replace(
                "kernel-lineage.json.partial",
                "kernel-lineage.json",
                src_dir_fd=held,
                dst_dir_fd=held,
            )
        finally:
            os.close(held)
        print("PASS " + key, flush=True)

    @staticmethod
    def context(session: str = "a") -> dict[str, str]:
        assert session in {"a", "b", "empty"}
        return {"project_id": PROJECT, "environment_id": ENVIRONMENT, "session_id": session}

    async def api(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        guard(self.head)
        allowed = {
            "/v1/environments",
            "/v1/executions",
            "/v1/provisioning",
            "/v1/kernels/lineage",
            "/v1/kernels/observe",
            "/v1/kernels/restart",
            f"/v1/environments/{ENVIRONMENT}",
        }
        assert method in {"GET", "POST"}
        assert path in allowed or re.fullmatch(
            r"/v1/(?:executions/exe|provisioning/prv)-[0-9a-f]{24}", path
        )
        async with self.http.request(
            method, URL + path, json=body, allow_redirects=False
        ) as response:
            raw = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                assert len(raw) + len(chunk) <= 1024 * 1024
                raw.extend(chunk)
            return response.status, json.loads(raw)

    async def success(self, method: str, path: str, body: Any = None) -> Any:
        status, value = await self.api(method, path, body)
        assert status in {200, 201}, (path, status, value)
        return value

    async def observe(self, session: str = "a") -> tuple[int, Any]:
        async with asyncio.timeout(10):
            status, value = await self.api(
                "POST", "/v1/kernels/lineage", {"context": self.context(session)}
            )
        assert status in {200, 503}, (status, value)
        if status == 200:
            checked = KernelLineageObservation.model_validate_json(json.dumps(value), strict=True)
            assert (checked.project_id, checked.environment_id, checked.session_id) == (
                PROJECT,
                ENVIRONMENT,
                session,
            )
        else:
            assert value["error"]["kind"] == "unavailable"
        return status, value

    def proved(self, observation: tuple[int, Any], session: str = "a") -> dict[str, Any]:
        status, value = observation
        assert status == 200
        assert len(value["kernels"]) == 1
        checked = KernelLineageObservation.model_validate_json(json.dumps(value), strict=True)
        entry = checked.kernels[0]
        assert entry.lineage_status == "proved"
        assert entry.birth is not None
        birth = entry.birth
        assert (birth.project_id, birth.environment_id, birth.session_id, birth.language) == (
            PROJECT,
            ENVIRONMENT,
            session,
            "python",
        )
        environment = self.values["environment-created"]
        assert (birth.sandbox, birth.sandbox_id) == (
            environment["sandbox"],
            environment["sandbox_id"],
        )
        retained = snapshot(self.head)["rows"]
        assert retained["endpoint"] == [birth.incarnation, birth.sandbox, birth.sandbox_id]
        assert birth.model_dump(mode="json") in retained["births"]
        generations = [row for row in retained["generations"] if row[:2] == [session, "python"]]
        assert len(generations) == 1
        _, _, incarnation, base, last = generations[0]
        assert incarnation == birth.incarnation
        assert base + birth.generation == entry.kernel.generation <= last
        return {
            "entry": entry.model_dump(mode="json"),
            "sha256": kernel_lineage_sha256(
                KernelLineageIdentity(birth=birth, generation=entry.kernel.generation)
            ),
        }

    async def cell(self, name: str, session: str, code: str, *, state: str = "succeeded") -> Any:
        value = await self.success(
            "POST",
            "/v1/executions",
            {
                "kind": "cell",
                "context": self.context(session),
                "language": "python",
                "operation_id": f"lab28-{name}-{STAMP}",
                "timeout_seconds": 120,
                "code": code,
            },
        )
        async with asyncio.timeout(150):
            while value["state"] not in TERMINAL:
                await asyncio.sleep(0.1)
                value = await self.success("GET", f"/v1/executions/{value['execution_id']}")
        assert value["state"] == state, value
        assert value["location"]["backend"] == "openshell"
        assert value["location"]["isolation_tier"] == 3
        self.save(name, value)
        return value

    async def before(self) -> None:
        assert not self.values
        assert guard(self.head)["openshell"]["environments"].get("suspend_after") is None
        environment = await self.success(
            "POST",
            "/v1/environments",
            {
                "project_id": PROJECT,
                "environment_id": ENVIRONMENT,
                "profile": "minimal",
                "policy": bounded("policy.digest", 128).decode().strip(),
            },
        )
        self.save("environment-created", environment)
        unavailable = await self.observe()
        assert unavailable[0] == 503
        self.save("before-first-execution-unavailable", unavailable)
        first = await self.cell(
            "initial",
            "a",
            "import json,pathlib,importlib.util\nlab28_value=1729\nprint(json.dumps({'value':lab28_value,'boot':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'networkx_present':importlib.util.find_spec('networkx') is not None}))",
        )
        identity = json.loads(first["result"]["stdout"]["text"])
        assert identity["value"] == 1729
        assert identity["networkx_present"] is False
        pin = self.proved(await self.observe())
        assert pin["entry"]["birth"]["start_revision"] == 0
        self.save("initial-pin", pin)
        operation = await self.success(
            "POST",
            "/v1/provisioning",
            {
                "operation_id": f"lab28-install-{STAMP}",
                "context": self.context(),
                "purpose": "Disposable passive lineage package revision acceptance",
                "conda": ["networkx"],
            },
        )
        async with asyncio.timeout(900):
            while operation["state"] != "finished":
                assert operation["state"] != "awaiting_permission", operation
                await asyncio.sleep(0.2)
                operation = await self.success(
                    "GET", f"/v1/provisioning/{operation['provision_id']}"
                )
        assert operation["outcome"] == "completed", operation
        self.save("install-finished", operation)
        observed = await self.observe()
        old = self.proved(observed)
        assert old["sha256"] == pin["sha256"]
        assert old["entry"]["kernel"]["revision_status"] == "predates"
        assert old["entry"]["kernel"]["current_revision"] > old["entry"]["birth"]["start_revision"]
        self.save("a-after-install", old)
        await self.cell("new-session", "b", "import networkx\nprint(networkx.__version__)")
        new = self.proved(await self.observe("b"), "b")
        assert new["entry"]["kernel"]["revision_status"] == "current"
        assert new["entry"]["birth"]["start_revision"] == old["entry"]["kernel"]["current_revision"]
        self.save("b-after-install", new)
        before = snapshot(self.head)
        repeated = [await self.observe() for _ in range(10)]
        assert all(self.proved(item) == old for item in repeated)
        official = await self.worker.observe_kernel_lineage(ExecutionContext(**self.context()))
        assert official.model_dump(mode="json")["kernels"] == [old["entry"]]
        status, plain = await self.api("POST", "/v1/kernels/observe", {"context": self.context()})
        assert status == 200
        assert plain["kernels"] == [old["entry"]["kernel"]]
        assert plain["current_revision"] == observed[1]["current_revision"]
        empty = await self.observe("empty")
        assert empty[0] == 200
        assert empty[1]["kernels"] == []
        status, denied = await self.api(
            "POST",
            "/v1/kernels/lineage",
            {"context": {**self.context(), "project_id": PROJECT + "b"}},
        )
        assert status == 403
        assert snapshot(self.head) == before
        self.save(
            "active-readonly",
            {
                "sql": before,
                "observations": repeated,
                "client": official.model_dump(mode="json"),
                "GUI-parity": plain,
                "empty": empty,
                "foreign": [status, denied],
            },
        )
        self.save(
            "unknown-boundary",
            {
                "runtime": "not_tested",
                "reason": "No missing/legacy history is manufactured; corrupted/unmanaged/current-unknown proof remains covered by local hostile tests.",
            },
        )
        self.save("before-complete", True)

    async def restarted(self) -> None:
        assert self.values.get("before-complete") is True
        assert "restarted-complete" not in self.values
        assert guard(self.head)["openshell"]["environments"].get("suspend_after") is None
        before = snapshot(self.head)
        pin = self.proved(await self.observe())
        assert pin == self.values["a-after-install"]
        assert self.proved(await self.observe("b"), "b") == self.values["b-after-install"]
        assert snapshot(self.head) == before
        same = await self.cell(
            "worker-restart-global",
            "a",
            "import json,pathlib\nprint(json.dumps({'value':lab28_value,'boot':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()}))",
        )
        actual = json.loads(same["result"]["stdout"]["text"])
        initial = json.loads(self.values["initial"]["result"]["stdout"]["text"])
        assert actual == {"value": 1729, "boot": initial["boot"]}
        self.save("worker-restart-continuity", {"pin": pin, "sql": before})
        await self.success(
            "POST", "/v1/kernels/restart", {"context": self.context(), "language": "python"}
        )
        fresh = self.proved(await self.observe())
        assert fresh["sha256"] != pin["sha256"]
        assert fresh["entry"]["birth"]["incarnation"] == pin["entry"]["birth"]["incarnation"]
        assert fresh["entry"]["birth"]["generation"] > pin["entry"]["birth"]["generation"]
        assert fresh["entry"]["kernel"]["revision_status"] == "current"
        gone = await self.cell(
            "explicit-restart-old-global-gone", "a", "print(lab28_value)", state="failed"
        )
        assert gone["result"]["error"]["name"] == "NameError"
        await self.cell("explicit-restart-new-global", "a", "lab28_value=2026;print(lab28_value)")
        assert self.proved(await self.observe()) == fresh
        self.save("explicit-restart-pin", fresh)
        self.save("restarted-complete", True)

    async def idle(self) -> None:
        assert self.values.get("restarted-complete") is True
        assert "idle-complete" not in self.values
        assert guard(self.head)["openshell"]["environments"].get("suspend_after") == 3
        await self.cell("idle-warmup", "a", "print(lab28_value)")
        warm = self.proved(await self.observe())
        assert warm == self.values["explicit-restart-pin"]
        self.save("idle-start", {"utc": datetime.now(UTC).isoformat(), "pin": warm})
        observations = []
        async with asyncio.timeout(90):
            while True:
                observations.append(await self.observe())
                environment = await self.success("GET", f"/v1/environments/{ENVIRONMENT}")
                if environment["state"] == "stopped":
                    break
                assert len(observations) < 400
                await asyncio.sleep(0.2)
        assert any(status == 200 for status, _ in observations)
        assert environment["sandbox_id"] == self.values["environment-created"]["sandbox_id"]
        before = snapshot(self.head)
        stopped = []
        for _ in range(30):
            stopped.append(await self.observe())
            assert stopped[-1][0] == 503
            assert await self.success("GET", f"/v1/environments/{ENVIRONMENT}") == environment
            await asyncio.sleep(0.2)
        try:
            await self.worker.observe_kernel_lineage(ExecutionContext(**self.context()))
        except WorkerAPIError as error:
            if error.kind != "unavailable":
                raise AssertionError("Unexpected stopped lineage error kind") from error
        else:
            raise AssertionError("Stopped lineage returned false empty success")
        assert snapshot(self.head) == before
        self.save(
            "idle-complete",
            {"environment": environment, "during": observations, "stopped": stopped, "sql": before},
        )
        print("Root must retain actual gateway Stopped proof before resume.", flush=True)

    async def resume(self) -> None:
        assert "idle-complete" in self.values
        assert "resume-complete" not in self.values
        raw, info = bounded_file(GATEWAY, 65536)
        proof = json.loads(raw)
        marker = json.loads(bounded(MARKER, 2048))
        assert proof["run_id"] == marker["run_id"]
        assert proof["source_head"] == self.head
        assert proof["helper_sha256"] == marker["helper_sha256"]
        old = self.values["environment-created"]
        assert (proof["name"], proof["id"], proof["phase"]) == (
            old["sandbox"],
            old["sandbox_id"],
            "Stopped",
        )
        stamp = info.st_mtime
        assert (
            datetime.fromisoformat(self.values["idle-start"]["utc"]).timestamp()
            <= stamp
            <= time.time()
        )
        assert (
            await self.success("GET", f"/v1/environments/{ENVIRONMENT}")
            == self.values["idle-complete"]["environment"]
        )
        assert (await self.observe())[0] == 503
        assert snapshot(self.head) == self.values["idle-complete"]["sql"]
        self.save(
            "gateway-stopped-proof",
            {
                "name": proof["name"],
                "id": proof["id"],
                "phase": proof["phase"],
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "run_id": marker["run_id"],
            },
        )
        resumed = await self.cell(
            "ordinary-resume",
            "a",
            "import json,pathlib\nprint(json.dumps({'old_global':'lab28_value' in globals(),'boot':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()}))",
        )
        actual = json.loads(resumed["result"]["stdout"]["text"])
        assert actual["old_global"] is False
        assert resumed["kernel_state_lost"] is True
        assert (
            actual["boot"] != json.loads(self.values["initial"]["result"]["stdout"]["text"])["boot"]
        )
        fresh = self.proved(await self.observe())
        prior = self.values["explicit-restart-pin"]
        assert fresh["sha256"] != prior["sha256"]
        assert fresh["entry"]["birth"]["incarnation"] != prior["entry"]["birth"]["incarnation"]
        assert fresh["entry"]["kernel"]["generation"] > prior["entry"]["kernel"]["generation"]
        assert fresh["entry"]["kernel"]["revision_status"] == "current"
        original = await self.success(
            "GET", f"/v1/executions/{self.values['initial']['execution_id']}"
        )
        assert original == self.values["initial"]
        self.save("ordinary-resume-pin", fresh)
        self.save("original-terminal-unchanged", original)
        self.save("resume-complete", True)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("init", "before", "restarted", "idle", "resume"))
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    guard(args.head, create=args.stage == "init")
    if args.stage == "init":
        print("Lab28 source/helper marker created; no API request")
        return
    token = bounded("worker/client-a.token", 4096).decode().strip()
    async with (
        aiohttp.ClientSession(
            headers={"Authorization": "Bearer " + token}, timeout=aiohttp.ClientTimeout(total=180)
        ) as http,
        WorkerClient(URL, token, session=http) as worker,
    ):
        await getattr(Checks(args.head, http, worker), args.stage)()


if __name__ == "__main__":
    asyncio.run(main())
