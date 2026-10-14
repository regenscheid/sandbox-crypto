# SPDX-License-Identifier: MIT
"""Recorded SQLite/blob fixtures and in-process HTTP only; no live worker or guest."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    CondaPackage,
    EnvironmentInfo,
    EnvironmentRevision,
    EnvironmentState,
    Lockfile,
    LockfileRef,
    PipDistribution,
)
from sandbox_science.contracts.lockfile_diffs import LockfileSelection
from sandbox_science.contracts.revision_packages import RevisionPackagePage
from sandbox_science.worker import lockfile_diffs as reader
from sandbox_science.worker.app import LOCKFILE_DIFFS, create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.environments import Environments
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.provisioner import Provisioner
from sandbox_science.worker.settings import ClientSettings, token_digest
from sandbox_science.worker.store import RevisionRecord, Store

NOW = datetime(2026, 10, 13, tzinfo=UTC)
TOKEN = "test-secret-revision-package-project-not-a-credential"
BASE = "/v1/environments/env/provisioning/observations/revisions"
HEADERS = {"Authorization": "Bearer " + TOKEN}


class Fixture:
    def __init__(self, root: Path) -> None:
        self.store = Store(root / "worker.sqlite3")
        self.blobs = Blobs(root / "lockfiles")
        self.info = EnvironmentInfo(
            environment_id="env",
            project_id="p",
            profile="minimal",
            image="synthetic",
            policy="sha256:" + "0" * 64,
            sandbox="synthetic",
            state=EnvironmentState.STOPPED,
            created_at=NOW,
            updated_at=NOW,
        )

    def seed(
        self,
        number: int,
        lock: Lockfile | bytes | None,
        *,
        conda: int = 0,
        pip: int = 0,
    ) -> LockfileSelection:
        ref = None
        if lock is not None:
            if isinstance(lock, Lockfile):
                conda, pip = len(lock.conda), len(lock.pip)
                raw = lock.model_dump_json().encode("utf-8")
            else:
                raw = lock
            digest = hashlib.sha256(raw).hexdigest()
            writer = self.blobs.writer()
            writer.write(raw)
            assert writer.commit(digest)
            ref = LockfileRef(
                sha256=digest, size=len(raw), conda_packages=conda, pip_distributions=pip
            )
        value = EnvironmentRevision(
            revision=number,
            fingerprint=f"{number:032x}",
            recorded_at=NOW,
            managed=False if number else None,
            lockfile=ref,
        )
        self.store.add_revision("env", RevisionRecord(number, value.fingerprint, ""), value)
        return LockfileSelection(
            revision=number,
            record_sha256=hashlib.sha256(value.model_dump_json().encode()).hexdigest(),
            lockfile=ref,
        )

    def select(self, selection: LockfileSelection) -> LockfileSelection:
        row = self.store.provision_observations.selected_revision(
            "p",
            "env",
            since=0,
            revision=selection.revision,
            pin=selection.record_sha256,
        )
        return LockfileSelection(
            revision=row.revision, record_sha256=row.record_sha256, lockfile=row.lockfile
        )

    def page(
        self, selection: LockfileSelection, offset: int = 0, limit: int = 8
    ) -> RevisionPackagePage:
        return RevisionPackagePage.model_validate(
            {
                "project_id": "p",
                "environment_id": "env",
                "observed_at": NOW,
                **reader.revision_packages(self.blobs.root, self.select(selection), offset, limit),
            },
            strict=True,
        )

    def app(self) -> web.Application:
        class Forbidden:
            async def forget(self, *args):
                raise AssertionError("no active environment cleanup")

            def __getattr__(self, name: str):
                raise AssertionError("no active component: " + name)

        environments = cast(
            Environments,
            SimpleNamespace(
                records=SimpleNamespace(get=lambda env: self.info if env == "env" else None),
                deleted=[],
                required_deleting=[],
                confirmed_deleted=[],
                reusing=[],
                required_deleted=[],
            ),
        )
        manager = cast(
            ExecutionManager,
            SimpleNamespace(
                store=self.store,
                clock=SYSTEM_CLOCK,
                revisions=SimpleNamespace(blobs=self.blobs),
                forget_revisions=lambda _: None,
                guard_package_deletion=lambda _: None,
                forget_package_gate=lambda _: None,
                guard_package_reuse=lambda _: None,
                end_streams=lambda: None,
            ),
        )
        clients = (ClientSettings(name="read", token_sha256=token_digest(TOKEN), projects=("p",)),)
        return create_app(
            manager,
            "test-secret-revision-package-pool-not-a-credential",
            environments,
            clients,
            cast(Provisioner, Forbidden()),
        )


@pytest.fixture
def fixture(tmp_path: Path):
    value = Fixture(tmp_path)
    try:
        yield value
    finally:
        value.store.close()


@pytest.mark.parametrize("revision", [0, 1])
def test_unchanged_agpl_base_and_unmanaged_package_without_operation(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, revision: int
):
    selection = fixture.seed(
        revision,
        Lockfile(
            conda=(CondaPackage(name="unchanged", version="1", build="0", license="AGPL-3.0"),),
            pip=(PipDistribution(name="Some_Pkg", version="1", location="user"),),
        ),
    )
    assert not reader.compare(fixture.blobs.root, (selection, selection), 0, 8)["differences"]
    scan, calls = reader._scan, []

    def counted(*args):
        calls.append(1)
        return scan(*args)

    monkeypatch.setattr(reader, "_scan", counted)
    monkeypatch.setattr(
        Lockfile, "model_validate_json", lambda *a, **k: pytest.fail("full lockfile parse")
    )
    changes = fixture.store._db.total_changes
    page = fixture.page(selection)
    assert calls == [1]
    assert page.total == 2
    conda, pip = page.packages
    assert conda.license.declared == "AGPL-3.0"
    assert conda.license.agpl is True
    assert pip.name == "some-pkg"
    assert pip.location == "user"
    assert pip.package.name == "Some_Pkg"
    assert pip.license.declared is None
    assert pip.license.agpl is None
    assert fixture.store._db.total_changes == changes
    assert not fixture.store._db.in_transaction


def test_exact_stateless_pages_preserve_namespace_location_order_and_all_fields(fixture: Fixture):
    selection = fixture.seed(
        0,
        Lockfile(
            conda=tuple(
                CondaPackage(
                    name=f"p{i:02}",
                    version="1",
                    build="build",
                    channel="conda-forge",
                    subdir="linux-64",
                    file=f"p{i:02}.conda",
                    sha256="a" * 64,
                    md5="b" * 32,
                    license=None if i % 2 else "MIT",
                )
                for i in range(17)
            ),
            pip=(
                PipDistribution(name="P_0", version="1", installer="uv", location="user"),
                PipDistribution(name="P_0", version="1", installer="pip", location="prefix"),
            ),
        ),
    )
    rows, offset = [], 0
    while True:
        page = fixture.page(selection, offset)
        assert page == fixture.page(selection, offset)
        assert page.total == 19
        assert len(page.model_dump_json().encode("utf-8")) <= 64 * 1024
        rows.extend(page.packages)
        if page.next_offset is None:
            break
        offset = page.next_offset
    assert [row.name for row in rows[:17]] == [f"p{i:02}" for i in range(17)]
    assert [(row.name, row.location) for row in rows[-2:]] == [("p-0", "prefix"), ("p-0", "user")]
    assert rows[0].package == CondaPackage(
        name="p00",
        version="1",
        build="build",
        channel="conda-forge",
        subdir="linux-64",
        file="p00.conda",
        sha256="a" * 64,
        md5="b" * 32,
        license="MIT",
    )
    assert rows[1].license.agpl is None
    assert fixture.page(selection, 19).packages == ()
    with pytest.raises(WorkerError, match="offset"):
        fixture.page(selection, 20)


def test_true_empty_is_complete_and_missing_is_held(fixture: Fixture):
    empty = fixture.page(fixture.seed(0, Lockfile()))
    assert empty.status == "complete"
    assert empty.total == 0
    assert empty.header is not None
    assert not empty.packages
    assert not empty.holds
    held = fixture.page(fixture.seed(1, None))
    assert held.status == "held"
    assert held.total is None
    assert held.holds == ("lockfile_unrecorded",)


@pytest.mark.parametrize("case", ["missing", "skipped", "format", "index_limit", "entry_limit"])
def test_conservative_held_sources_do_not_claim_empty(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, case: str
):
    lock = Lockfile(
        conda=(CondaPackage(name="pkg", version="1", build="0"),), skipped=int(case == "skipped")
    )
    raw = lock.model_dump_json().encode()
    if case == "format":
        raw = raw.replace(b'"version":1', b'"version":2', 1)
    selection = fixture.seed(0, raw, conda=1)
    assert selection.lockfile is not None
    if case == "missing":
        fixture.blobs.path(selection.lockfile.sha256).unlink()
    elif case == "index_limit":
        monkeypatch.setattr(reader, "MAX_INDEX", 1)
    elif case == "entry_limit":
        monkeypatch.setattr(reader, "MAX_ENTRY", 10)
    page = fixture.page(selection)
    assert page.status == "held"
    assert page.total is None
    assert page.holds
    assert not page.packages
    assert page.next_offset is None


@pytest.mark.parametrize(
    "case",
    ["duplicate", "escaped_duplicate", "string_entry", "bool", "float", "count", "credential"],
)
def test_hostile_retained_bytes_refuse(fixture: Fixture, case: str):
    raw = Lockfile(conda=(CondaPackage(name="pkg", version="1", build="0"),)).model_dump_json()
    if case == "duplicate":
        raw = raw.replace('"version":1', '"version":2,"version":1', 1)
    elif case == "escaped_duplicate":
        raw = raw.replace('"skipped":0', '"skipped":0,"\\u0073kipped":0')
    elif case in ("bool", "float"):
        raw = raw.replace('"version":1', '"version":true' if case == "bool" else '"version":1.0', 1)
    elif case == "string_entry":
        value = json.loads(raw)
        value["conda"][0] = json.dumps(value["conda"][0])
        raw = json.dumps(value)
    elif case == "credential":
        value = json.loads(raw)
        value["conda"][0]["channel"] = "https://test-secret-not-a-credential@example.test/path"
        raw = json.dumps(value)
    selection = fixture.seed(0, raw.encode(), conda=2 if case == "count" else 1)
    with pytest.raises(WorkerError, match="unavailable"):
        fixture.page(selection)


@pytest.mark.parametrize("case", ["hash", "size", "symlink", "fifo", "hardlink"])
def test_blob_proof_refuses_mismatch_and_special_files_without_fd_leak(fixture: Fixture, case: str):
    selection = fixture.seed(0, Lockfile(conda=(CondaPackage(name="pkg", version="1", build="0"),)))
    assert selection.lockfile is not None
    path = fixture.blobs.path(selection.lockfile.sha256)
    if case in ("hash", "size"):
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b'"pkg"', b'"bad"') if case == "hash" else raw + b" ")
    else:
        original = path.with_suffix(".original")
        path.rename(original)
        if case == "symlink":
            path.symlink_to(original)
        elif case == "fifo":
            os.mkfifo(path)
        else:
            os.link(original, path)
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(WorkerError, match="unavailable"):
        fixture.page(selection)
    assert len(os.listdir("/proc/self/fd")) == before


def test_huge_entry_holds_before_full_decode(fixture: Fixture, monkeypatch: pytest.MonkeyPatch):
    raw = (
        Lockfile()
        .model_dump_json()
        .replace('"conda":[]', '"conda":[{"name":"' + "x" * 2_000_000 + '"}]')
    )
    selection = fixture.seed(0, raw.encode(), conda=1)
    decode = reader._decode

    def bounded(raw: bytes):
        assert len(raw) <= reader.MAX_ENTRY
        return decode(raw)

    monkeypatch.setattr(reader, "_decode", bounded)
    tracemalloc.start()
    try:
        page = fixture.page(selection)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert page.status == "held"
    assert page.holds == ("input_limit",)
    assert peak < 300_000  # This fixture's Python allocation observation, not an RSS bound.


@pytest.mark.asyncio
async def test_shared_physical_owner_across_inventory_diff_cancel_deadline_and_close(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
):
    selection = fixture.seed(0, Lockfile())
    started, release = threading.Event(), threading.Event()
    descriptors: list[int] = []
    read = reader._read_chunk

    def blocked(fd: int) -> bytes:
        descriptors.append(fd)
        started.set()
        assert release.wait(5)
        return read(fd)

    monkeypatch.setattr(reader, "_read_chunk", blocked)
    monkeypatch.setattr(reader, "DEADLINE", 0.01)
    owner = reader.LockfileDiffs()
    task = asyncio.create_task(
        owner.read_packages("env", fixture.blobs.root, lambda: selection, lambda _: None, 0, 8)
    )
    try:
        while not started.is_set():
            await asyncio.sleep(0)
        with pytest.raises(WorkerError, match="unavailable"):
            await task
        assert len(owner.tasks) == 1
        assert owner.environments == {"env"}
        os.fstat(descriptors[0])

        def forbidden():
            pytest.fail("same environment refusal must precede selection")

        with pytest.raises(WorkerError):
            await owner.read("env", fixture.blobs.root, forbidden, lambda _: None, 0, 8)
        monkeypatch.setattr(reader, "DEADLINE", 5)
        other = asyncio.create_task(
            owner.read(
                "other", fixture.blobs.root, lambda: (selection, selection), lambda _: None, 0, 8
            )
        )
        while len(descriptors) < 2:
            await asyncio.sleep(0)
        with pytest.raises(WorkerError):
            await owner.read_packages("third", fixture.blobs.root, forbidden, lambda _: None, 0, 8)
        other.cancel()
        other.cancel()
        with pytest.raises(asyncio.CancelledError):
            await other
        assert len(owner.tasks) == 2
        close = asyncio.create_task(owner.close())
        await asyncio.sleep(0)
        close.cancel()
        await asyncio.sleep(0)
        close.cancel()
        assert not close.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await close
        assert not owner.tasks
        assert not owner.environments
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptors[0])
    finally:
        release.set()
        await owner.close()


@pytest.mark.asyncio
async def test_selected_pin_recheck_after_read_refuses_changed_record(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
):
    selection = fixture.seed(0, Lockfile())
    started, release = threading.Event(), threading.Event()
    original = reader.revision_packages

    def paused(*args):
        result = original(*args)
        started.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(reader, "revision_packages", paused)
    owner = reader.LockfileDiffs()

    def prepare():
        return fixture.select(selection)

    def verify(original: LockfileSelection) -> None:
        assert prepare() == original

    task = asyncio.create_task(
        owner.read_packages("env", fixture.blobs.root, prepare, verify, 0, 8)
    )
    try:
        while not started.is_set():
            await asyncio.sleep(0)
        fixture.store._db.execute("UPDATE revision_entries SET record=record||' ' WHERE revision=0")
        release.set()
        with pytest.raises(WorkerError, match="record changed"):
            await task
    finally:
        release.set()
        await owner.close()
    assert not fixture.store._db.in_transaction


@pytest.mark.parametrize(
    ("offset", "limit"), [(True, 8), (0, False), (0.0, 8), (40_001, 8), (0, 0), (0, 9)]
)
def test_direct_page_parameter_types_refuse(fixture: Fixture, offset: int, limit: int):
    with pytest.raises(WorkerError, match="selection"):
        reader.revision_packages(fixture.blobs.root, fixture.seed(0, Lockfile()), offset, limit)


@pytest.mark.parametrize(
    "case", ["header", "total", "next", "license", "location", "order", "held"]
)
def test_raw_json_page_coherence_refuses(fixture: Fixture, case: str):
    selection = fixture.seed(
        0,
        Lockfile(
            conda=(
                CondaPackage(name="a", version="1", build="0", license="MIT"),
                CondaPackage(name="b", version="1", build="0"),
            )
        ),
    )
    value = fixture.page(selection).model_dump(mode="json")
    if case == "header":
        value["header"]["sha256"] = "0" * 64
    elif case == "total":
        value["total"] = 1
    elif case == "next":
        value["next_offset"] = 1
    elif case == "license":
        value["packages"][0]["license"]["declared"] = "AGPL-3.0"
    elif case == "location":
        value["packages"][0]["location"] = "user"
    elif case == "order":
        value["packages"].reverse()
    else:
        value.update(status="held", holds=["blob_missing"])
    with pytest.raises(ValidationError):
        RevisionPackagePage.model_validate_json(json.dumps(value), strict=True)


@pytest.mark.asyncio
async def test_actual_http_scope_query_current_head_independence_and_no_wake(fixture: Fixture):
    selected = fixture.seed(
        0,
        Lockfile(conda=tuple(CondaPackage(name=f"p{i}", version="1", build="0") for i in range(9))),
    )
    fixture.seed(1, Lockfile())  # Advancing current head does not replace the selected source.
    query = f"project_id=p&pin={selected.record_sha256}"
    async with TestClient(TestServer(fixture.app())) as client:
        response = await client.get(BASE + "/0/packages?" + query, headers=HEADERS)
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        page = RevisionPackagePage.model_validate_json(await response.read(), strict=True)
        assert page.selection == selected
        assert page.total == 9
        assert page.next_offset == 8
        second = await client.get(BASE + "/0/packages?" + query + "&offset=8", headers=HEADERS)
        assert [row["name"] for row in (await second.json())["packages"]] == ["p8"]
        for suffix, status in (
            ("&limit=0", 400),
            ("&limit=9", 400),
            ("&offset=00", 400),
            ("&pin=" + "0" * 64, 400),
            ("&extra=1", 400),
        ):
            result = await client.get(BASE + "/0/packages?" + query + suffix, headers=HEADERS)
            assert result.status == status
            assert result.headers["Cache-Control"] == "no-store"

        for path, status, headers in (
            (BASE + "/00/packages?" + query, 400, HEADERS),
            (BASE + "/0/packages?" + query.replace(selected.record_sha256, "0" * 64), 409, HEADERS),
            (BASE + "/2/packages?" + query, 404, HEADERS),
            (BASE + "/0/packages?" + query.replace("project_id=p", "project_id=q"), 404, HEADERS),
            (BASE.replace("/env/", "/foreign/") + "/0/packages?" + query, 404, HEADERS),
            (BASE + "/0/packages?" + query, 401, {}),
        ):
            result = await client.get(path, headers=headers)
            assert result.status == status
            assert result.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["foreign", "recreated"])
async def test_actual_http_scope_recheck_after_owned_file_work(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, change: str
):
    selected = fixture.seed(0, Lockfile())
    started, release = threading.Event(), threading.Event()
    original = reader.revision_packages

    def paused(*args):
        result = original(*args)
        started.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(reader, "revision_packages", paused)
    async with TestClient(TestServer(fixture.app())) as client:
        pending = asyncio.create_task(
            client.get(
                BASE + f"/0/packages?project_id=p&pin={selected.record_sha256}", headers=HEADERS
            )
        )
        try:
            while not started.is_set():
                await asyncio.sleep(0)
            fixture.info = fixture.info.model_copy(
                update={"project_id": "q"}
                if change == "foreign"
                else {"created_at": NOW + timedelta(seconds=1)}
            )
            release.set()
            response = await pending
            assert response.status == (404 if change == "foreign" else 409)
            assert response.headers["Cache-Control"] == "no-store"
            assert not fixture.store._db.in_transaction
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_actual_http_disconnect_keeps_physical_fd_and_slot_until_drain(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
):
    selected = fixture.seed(0, Lockfile())
    started, release = threading.Event(), threading.Event()
    descriptors: list[int] = []
    original = reader._read_chunk

    def blocked(fd: int) -> bytes:
        descriptors.append(fd)
        started.set()
        assert release.wait(5)
        return original(fd)

    monkeypatch.setattr(reader, "_read_chunk", blocked)
    app = fixture.app()
    async with TestClient(TestServer(app)) as client:
        path = BASE + f"/0/packages?project_id=p&pin={selected.record_sha256}"
        pending = asyncio.create_task(client.get(path, headers=HEADERS))
        try:
            while not started.is_set():
                await asyncio.sleep(0)
            pending.cancel()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            owner = app[LOCKFILE_DIFFS]
            assert len(owner.tasks) == 1
            assert owner.environments == {"env"}
            os.fstat(descriptors[0])
            refused = await client.get(path, headers=HEADERS)
            assert refused.status == 503
            assert (
                await asyncio.wait_for(asyncio.to_thread(lambda: "default lane free"), 0.5)
                == "default lane free"
            )
            release.set()
            while owner.tasks:
                await asyncio.sleep(0)
            assert not owner.environments
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(descriptors[0])
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
