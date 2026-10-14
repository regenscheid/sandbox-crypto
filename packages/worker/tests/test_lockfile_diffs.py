# SPDX-License-Identifier: MIT
"""Synthetic retained bytes only; no guest, provider, model or live worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import threading
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sandbox_science.contracts import (
    CondaPackage,
    EnvironmentRevision,
    Lockfile,
    LockfileRef,
    PipDistribution,
)
from sandbox_science.contracts.lockfile_diffs import LockfileDiffPage, LockfileSelection
from sandbox_science.worker import lockfile_diffs as diff
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.store import RevisionRecord, Store

NOW = datetime(2026, 10, 13, tzinfo=UTC)


class Fixture:
    def __init__(self, root: Path) -> None:
        self.store = Store(root / "worker.sqlite3")
        self.blobs = Blobs(root / "lockfiles")

    def seed(
        self, number: int, lock: Lockfile | bytes | None, *, conda: int = 0, pip: int = 0
    ) -> LockfileSelection:
        ref = None
        if lock is not None:
            if isinstance(lock, Lockfile):
                conda, pip = len(lock.conda), len(lock.pip)
                raw = lock.model_dump_json().encode()
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
            revision=number, fingerprint=f"{number:032x}", recorded_at=NOW, lockfile=ref
        )
        self.store.add_revision("env", RevisionRecord(number, value.fingerprint, ""), value)
        return LockfileSelection(
            revision=number,
            record_sha256=hashlib.sha256(value.model_dump_json().encode()).hexdigest(),
            lockfile=ref,
        )

    def pair(self, before: LockfileSelection, after: LockfileSelection) -> diff.Pair:
        rows = self.store.provision_observations.selected_pair(
            "p",
            "env",
            since=0,
            before=(before.revision, before.record_sha256),
            after=(after.revision, after.record_sha256),
        )
        selections = tuple(
            LockfileSelection(
                revision=row.revision, record_sha256=row.record_sha256, lockfile=row.lockfile
            )
            for row in rows
        )
        return selections[0], selections[1]

    def page(
        self, before: LockfileSelection, after: LockfileSelection, offset: int = 0, limit: int = 8
    ) -> LockfileDiffPage:
        return LockfileDiffPage.model_validate(
            {
                "project_id": "p",
                "environment_id": "env",
                "observed_at": NOW,
                **diff.compare(self.blobs.root, self.pair(before, after), offset, limit),
            },
            strict=True,
        )


@pytest.fixture
def fixture(tmp_path: Path):
    value = Fixture(tmp_path)
    try:
        yield value
    finally:
        value.store.close()


def test_pilot_exact_empty_and_full_metadata_changes(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
):
    a = fixture.seed(
        0,
        Lockfile(
            conda=(
                CondaPackage(
                    name="pkg",
                    version="1",
                    build="0",
                    channel="conda-forge",
                    sha256="a" * 64,
                    license="MIT",
                ),
            )
        ),
    )
    b = fixture.seed(
        1,
        Lockfile(
            conda=(
                CondaPackage(
                    name="pkg",
                    version="1",
                    build="0",
                    channel="other",
                    sha256="b" * 64,
                    license="AGPL-3.0",
                ),
            ),
            pip=(PipDistribution(name="pkg", version="1"),),
        ),
    )
    monkeypatch.setattr(
        Lockfile, "model_validate_json", lambda *a, **k: pytest.fail("full lockfile decode")
    )
    page = fixture.page(a, b)
    assert page.status == "complete"
    assert page.total == 2
    row, pip = page.differences
    assert row.changed_fields == ("channel", "license", "sha256")
    assert row.after_license is not None
    assert row.after_license.agpl is True
    assert pip.after_license is not None
    assert pip.after_license.declared is None
    empty = fixture.seed(
        2,
        b'{"format":"sandbox-science-lockfile","version":1,"python":null,"platform":null,"conda":[],"pip":[],"skipped":0}',
    )
    assert fixture.page(empty, empty).total == 0


def test_pip_identity_keeps_normalization_and_prefix_user(fixture: Fixture):
    a = fixture.seed(
        0,
        Lockfile(
            pip=(
                PipDistribution(name="Some_Pkg", version="1"),
                PipDistribution(name="same", version="1", location="user"),
            )
        ),
    )
    b = fixture.seed(
        1,
        Lockfile(
            pip=(
                PipDistribution(name="some-pkg", version="1", installer="uv"),
                PipDistribution(name="same", version="1"),
            )
        ),
    )
    page = fixture.page(a, b)
    assert page.total == 3
    assert [(r.name, r.location, r.change) for r in page.differences] == [
        ("same", "prefix", "added"),
        ("some-pkg", "prefix", "changed"),
        ("same", "user", "removed"),
    ]
    assert all(r.after_license is None or r.after_license.agpl is None for r in page.differences)


def test_deterministic_stateless_pages_and_full_envelope(fixture: Fixture):
    a = fixture.seed(0, Lockfile())
    b = fixture.seed(
        1,
        Lockfile(
            conda=tuple(
                CondaPackage(
                    name=f"pkg{i:02}",
                    version="1",
                    build="0",
                    channel="https://example.test/" + "a" * 1900,
                )
                for i in range(19)
            )
        ),
    )
    rows = []
    offset = 0
    while True:
        page = fixture.page(a, b, offset)
        assert len(page.model_dump_json().encode()) <= 64 * 1024
        assert page.total == 19
        rows += [r.name for r in page.differences]
        if page.next_offset is None:
            break
        offset = page.next_offset
    assert rows == [f"pkg{i:02}" for i in range(19)]
    assert fixture.page(a, b) == fixture.page(a, b)


@pytest.mark.parametrize(
    "case", ["unrecorded", "missing", "skipped", "format", "entry_limit", "index_limit"]
)
def test_explicit_held_never_means_empty(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, case: str
):
    lock = Lockfile(
        conda=(CondaPackage(name="pkg", version="1", build="0"),), skipped=int(case == "skipped")
    )
    raw = lock.model_dump_json().encode()
    if case in ("format", "version_bool", "version_float"):
        raw = raw.replace(
            b'"version":1',
            {
                "format": b'"version":2',
                "version_bool": b'"version":true',
                "version_float": b'"version":1.0',
            }[case],
        )
    a = fixture.seed(0, None if case == "unrecorded" else raw, conda=1)
    b = fixture.seed(1, Lockfile())
    if case == "missing":
        assert a.lockfile is not None
        fixture.blobs.path(a.lockfile.sha256).unlink()
    if case == "entry_limit":
        monkeypatch.setattr(diff, "MAX_ENTRY", 32)
    if case == "index_limit":
        monkeypatch.setattr(diff, "MAX_INDEX", 10)
    page = fixture.page(a, b)
    assert page.status == "held"
    assert page.total is None
    assert not page.differences
    assert page.holds


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_key",
        "escaped_key",
        "string_entry",
        "duplicate_conda",
        "case_conda",
        "duplicate_pip",
        "nested",
        "hash",
        "count",
        "credential",
        "size",
        "version_bool",
        "version_float",
    ],
)
def test_corrupt_ambiguous_sources_refuse(fixture: Fixture, case: str):
    value = Lockfile(
        conda=(CondaPackage(name="pkg", version="1", build="0"),),
        pip=(PipDistribution(name="pip-pkg", version="1"),),
    )
    raw = value.model_dump_json()
    conda, pip = 1, 1
    if case in ("version_bool", "version_float"):
        raw = raw.replace(
            '"version":1', '"version":true' if case == "version_bool" else '"version":1.0', 1
        )
    elif case == "duplicate_key":
        raw = raw.replace('"version":1', '"version":2,"version":1', 1)
    elif case == "escaped_key":
        raw = raw.replace('"skipped":0', '"skipped":0,"\\u0073kipped":0')
    elif case in (
        "string_entry",
        "duplicate_conda",
        "case_conda",
        "duplicate_pip",
        "nested",
        "credential",
    ):
        fields = json.loads(raw)
        if case == "string_entry":
            fields["conda"][0] = json.dumps(fields["conda"][0])
        elif case in ("duplicate_conda", "case_conda"):
            fields["conda"].append(
                {**fields["conda"][0], "name": "Pkg" if case == "case_conda" else "pkg"}
            )
            conda = 2
        elif case == "duplicate_pip":
            fields["pip"].append({**fields["pip"][0], "name": "PIP_pkg"})
            pip = 2
        elif case == "nested":
            fields["conda"][0]["license"] = {"nested": {"deep": {"extra": {"x": 1}}}}
        else:
            fields["pip"][0]["url"] = "https://test-secret-not-a-credential@example.test/file"
        raw = json.dumps(fields)
    a = fixture.seed(0, raw.encode(), conda=conda + int(case == "count"), pip=pip)
    b = fixture.seed(1, Lockfile())
    assert a.lockfile is not None
    if case in ("hash", "size"):
        path = fixture.blobs.path(a.lockfile.sha256)
        data = path.read_bytes()
        path.write_bytes(data.replace(b'"pkg"', b'"bad"') if case == "hash" else data + b" ")
    with pytest.raises(WorkerError, match="unavailable"):
        fixture.page(a, b)


def test_raw_revision_pin_changes_and_no_read_mutations(fixture: Fixture):
    a = fixture.seed(0, Lockfile())
    b = fixture.seed(1, Lockfile())
    changes = fixture.store._db.total_changes
    fixture.page(a, b)
    assert fixture.store._db.total_changes == changes
    assert not fixture.store._db.in_transaction
    fixture.store._db.execute("UPDATE revision_entries SET record=record||' ' WHERE revision=1")
    with pytest.raises(WorkerError, match="record changed"):
        fixture.page(a, b)
    assert not fixture.store._db.in_transaction


@pytest.mark.parametrize("kind", ["symlink", "fifo", "hardlink", "replacement"])
def test_nofollow_regular_identity_and_no_descriptor_leak(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, kind: str
):
    a = fixture.seed(0, Lockfile())
    b = fixture.seed(1, Lockfile(python="3.13"))
    assert a.lockfile is not None
    path = fixture.blobs.path(a.lockfile.sha256)
    original = path.with_suffix(".original")
    if kind != "replacement":
        path.rename(original)
        if kind == "symlink":
            path.symlink_to(original)
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            os.link(original, path)
    else:
        scan = diff._scan
        calls = 0

        def changed(*args, **kwargs):
            nonlocal calls
            result = scan(*args, **kwargs)
            calls += 1
            if calls == 1:
                root = fixture.blobs.root
                old = root.with_name("previous-lockfiles")
                root.rename(old)
                shutil.copytree(old, root)
            return result

        monkeypatch.setattr(diff, "_scan", changed)
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(WorkerError, match="unavailable"):
        fixture.page(a, b)
    assert len(os.listdir("/proc/self/fd")) == before


def test_huge_entry_stops_before_json_parse(fixture: Fixture, monkeypatch: pytest.MonkeyPatch):
    raw = (
        Lockfile()
        .model_dump_json()
        .replace('"conda":[]', '"conda":[{"name":"' + "x" * 2_000_000 + '"}]')
    )
    a = fixture.seed(0, raw.encode(), conda=1)
    b = fixture.seed(1, Lockfile())
    decode = diff._decode

    def guarded(value):
        assert len(value) <= diff.MAX_ENTRY
        return decode(value)

    monkeypatch.setattr(diff, "_decode", guarded)
    tracemalloc.start()
    try:
        page = fixture.page(a, b)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert page.status == "held"
    assert peak < 300000


@pytest.mark.asyncio
async def test_repeated_cancel_timeout_and_shutdown_keep_actual_lane(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
):
    a, b = fixture.seed(0, Lockfile()), fixture.seed(1, Lockfile())
    started, release = threading.Event(), threading.Event()
    read = diff._read_chunk
    descriptors: list[int] = []

    def blocked(fd: int) -> bytes:
        descriptors.append(fd)
        started.set()
        assert release.wait(5)
        return read(fd)

    monkeypatch.setattr(diff, "_read_chunk", blocked)
    owner = diff.LockfileDiffs()

    def prepare() -> diff.Pair:
        return fixture.pair(a, b)

    task = asyncio.create_task(owner.read("env", fixture.blobs.root, prepare, lambda _: None, 0, 8))
    while not started.is_set():
        await asyncio.sleep(0)
    task.cancel()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(owner.tasks) == 1
    assert owner.environments == {"env"}
    os.fstat(descriptors[0])
    with pytest.raises(WorkerError):
        await owner.read("env", fixture.blobs.root, prepare, lambda _: None, 0, 8)
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


@pytest.mark.asyncio
async def test_deadline_retains_actual_work_until_shutdown(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
):
    a, b = fixture.seed(0, Lockfile()), fixture.seed(1, Lockfile())
    started, release = threading.Event(), threading.Event()
    compare = diff.compare

    def blocked(*args):
        started.set()
        assert release.wait(5)
        return compare(*args)

    monkeypatch.setattr(diff, "compare", blocked)
    monkeypatch.setattr(diff, "DEADLINE", 0.01)
    owner = diff.LockfileDiffs()
    try:
        with pytest.raises(WorkerError, match="unavailable"):
            await owner.read(
                "env", fixture.blobs.root, lambda: fixture.pair(a, b), lambda _: None, 0, 8
            )
        assert started.is_set()
        assert owner.tasks
    finally:
        release.set()
        await owner.close()


@pytest.mark.asyncio
async def test_final_pair_recheck_refuses_concurrent_revision_change(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
):
    a, b = fixture.seed(0, Lockfile()), fixture.seed(1, Lockfile())
    started, release = threading.Event(), threading.Event()
    original = diff.compare

    def paused(*args):
        result = original(*args)
        started.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(diff, "compare", paused)
    owner = diff.LockfileDiffs()

    def verify(pair):
        assert fixture.pair(a, b) == pair

    task = asyncio.create_task(
        owner.read("env", fixture.blobs.root, lambda: fixture.pair(a, b), verify, 0, 8)
    )
    try:
        while not started.is_set():
            await asyncio.sleep(0)
        fixture.store._db.execute("UPDATE revision_entries SET record=record||' ' WHERE revision=1")
        release.set()
        with pytest.raises(WorkerError, match="record changed"):
            await task
    finally:
        release.set()
        await owner.close()
    assert not fixture.store._db.in_transaction


@pytest.mark.asyncio
async def test_global_quota_precedes_selection_and_releases_only_after_work(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
):
    a, b = fixture.seed(0, Lockfile()), fixture.seed(1, Lockfile())
    release = threading.Event()
    started = 0
    original = diff.compare

    def paused(*args):
        nonlocal started
        started += 1
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(diff, "compare", paused)
    owner = diff.LockfileDiffs()

    def prepare():
        return fixture.pair(a, b)

    tasks = [
        asyncio.create_task(owner.read(env, fixture.blobs.root, prepare, lambda _: None, 0, 8))
        for env in ("one", "two")
    ]
    try:
        while started < 2:
            await asyncio.sleep(0)

        def forbidden():
            pytest.fail("quota must precede SQLite selection")

        with pytest.raises(WorkerError):
            await owner.read("three", fixture.blobs.root, forbidden, lambda _: None, 0, 8)
    finally:
        release.set()
        await asyncio.gather(*tasks)
        await owner.close()
    assert not owner.tasks


@pytest.mark.asyncio
async def test_authenticated_actual_http_no_active_calls_and_exact_pages(fixture: Fixture):
    from types import SimpleNamespace
    from typing import cast

    from aiohttp.test_utils import TestClient, TestServer

    from sandbox_science.contracts import EnvironmentInfo, EnvironmentState
    from sandbox_science.worker.app import create_app
    from sandbox_science.worker.environments import Environments
    from sandbox_science.worker.manager import ExecutionManager
    from sandbox_science.worker.provisioner import Provisioner
    from sandbox_science.worker.settings import ClientSettings, token_digest

    a = fixture.seed(0, Lockfile())
    b = fixture.seed(
        1,
        Lockfile(conda=tuple(CondaPackage(name=f"p{i}", version="1", build="0") for i in range(9))),
    )
    info = EnvironmentInfo(
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

    class Records:
        def get(self, environment):
            return info if environment == "env" else None

    class Forbidden:
        async def forget(self, *args):
            raise AssertionError("no environment effect")

        def __getattr__(self, name):
            raise AssertionError("no active component: " + name)

    environments = cast(
        Environments,
        SimpleNamespace(
            records=Records(),
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
            store=fixture.store,
            revisions=SimpleNamespace(blobs=fixture.blobs),
            forget_revisions=lambda _: None,
            guard_package_deletion=lambda _: None,
            forget_package_gate=lambda _: None,
            guard_package_reuse=lambda _: None,
            end_streams=lambda: None,
        ),
    )
    secret = "test-secret-lockfile-diff-project-not-a-credential"
    clients = (ClientSettings(name="read", token_sha256=token_digest(secret), projects=("p",)),)
    app = create_app(
        manager,
        "test-secret-lockfile-diff-pool-not-a-credential",
        environments,
        clients,
        cast(Provisioner, Forbidden()),
    )
    base = "/v1/environments/env/provisioning/observations/lockfile-diff"
    query = (
        f"project_id=p&before=0&before_pin={a.record_sha256}&after=1&after_pin={b.record_sha256}"
    )
    headers = {"Authorization": "Bearer " + secret}
    async with TestClient(TestServer(app)) as client:
        result = await client.get(base + "?" + query, headers=headers)
        assert result.status == 200
        assert result.headers["Cache-Control"] == "no-store"
        value = LockfileDiffPage.model_validate_json(await result.read(), strict=True)
        assert value.total == 9
        assert len(value.differences) == 8
        assert value.next_offset == 8
        page = await client.get(base + "?" + query + "&offset=8", headers=headers)
        assert len((await page.json())["differences"]) == 1
        for suffix, expected in (
            ("&limit=0", 400),
            ("&limit=9", 400),
            ("&before=0", 400),
            ("&unknown=1", 400),
            ("&offset=00", 400),
        ):
            response = await client.get(base + "?" + query + suffix, headers=headers)
            assert response.status == expected
        denied = await client.get(base + "?" + query)
        assert denied.status == 401
        assert denied.headers["Cache-Control"] == "no-store"
        foreign = await client.get(
            base + "?" + query.replace("project_id=p", "project_id=q"), headers=headers
        )
        assert foreign.status == 404
        stale = await client.get(
            base + "?" + query.replace(b.record_sha256, "0" * 64), headers=headers
        )
        assert stale.status == 409


def test_representative_sparse_change_stream_trial(fixture: Fixture):
    packages = tuple(CondaPackage(name=f"pkg{i:04}", version="1", build="0") for i in range(1000))
    a = fixture.seed(0, Lockfile(conda=packages))
    b = fixture.seed(
        1, Lockfile(conda=(*packages[:-1], packages[-1].model_copy(update={"build": "1"})))
    )
    tracemalloc.start()
    try:
        page = fixture.page(a, b)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert page.total == 1
    assert page.differences[0].name == "pkg0999"
    assert page.differences[0].changed_fields == ("build",)
    assert (
        peak < 4 * 1024 * 1024
    )  # This fixture's Python allocation observation, not an RSS guarantee.


def test_small_section_quota_stops_before_unselected_tail(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
):
    a = fixture.seed(
        0,
        Lockfile(conda=tuple(CondaPackage(name=f"p{i}", version="1", build="0") for i in range(5))),
    )
    b = fixture.seed(1, Lockfile())
    monkeypatch.setattr(diff, "MAX_SECTION", 2)
    original = diff._package
    names = []

    def bounded(raw, kind):
        result = original(raw, kind)
        names.append(result.name)
        return result

    monkeypatch.setattr(diff, "_package", bounded)
    page = fixture.page(a, b)
    assert page.status == "held"
    assert names == ["p0", "p1", "p2"]


@pytest.mark.parametrize(("offset", "limit"), [(True, 8), (-1, 8), (0, False), (0, 0), (0, 9)])
def test_direct_page_preflight_precedes_files(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, offset: int, limit: int
):
    a, b = fixture.seed(0, Lockfile()), fixture.seed(1, Lockfile())
    monkeypatch.setattr(diff, "_Blob", lambda *args: pytest.fail("invalid page reached file open"))
    with pytest.raises(WorkerError, match="selection"):
        diff.compare(fixture.blobs.root, (a, b), offset, limit)


@pytest.mark.parametrize(
    ("field", "value"),
    [("sha256", "../foreign"), ("conda_packages", True), ("pip_distributions", 20_001)],
)
def test_copied_ref_cannot_bypass_path_or_count_preflight(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, field: str, value: object
):
    a, b = fixture.seed(0, Lockfile()), fixture.seed(1, Lockfile())
    assert a.lockfile is not None
    forged = a.model_copy(update={"lockfile": a.lockfile.model_copy(update={field: value})})
    monkeypatch.setattr(
        os, "open", lambda *args, **kwargs: pytest.fail("forged ref reached file open")
    )
    with pytest.raises(WorkerError, match="unavailable"):
        diff.compare(fixture.blobs.root, (forged, b), 0, 8)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sha256", "0" * 64),
        ("size", 1),
        ("conda_packages", 1),
        ("pip_distributions", 1),
        ("lockfile", None),
    ],
)
def test_held_response_still_binds_every_available_header(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, field: str, value: object
):
    a = fixture.seed(0, Lockfile())
    b = fixture.seed(1, Lockfile(conda=(CondaPackage(name="pkg", version="1", build="0"),)))
    monkeypatch.setattr(diff, "MAX_ENTRY", 32)
    page = fixture.page(a, b)
    assert page.status == "held"
    assert page.before_header is not None
    raw = json.loads(page.model_dump_json())
    if field == "lockfile":
        raw["before"][field] = value
    else:
        raw["before_header"][field] = value
    with pytest.raises(ValueError, match="header does not bind"):
        LockfileDiffPage.model_validate_json(json.dumps(raw), strict=True)
