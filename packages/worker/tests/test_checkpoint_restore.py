# SPDX-License-Identifier: MIT
# ruff: noqa: F811 - the fixtures shared with test_checkpoint_jobs are parameters here
"""Restoring a kept checkpoint into a fresh environment, offline against the fake store
the checkpoint tests use: the tree digest, the plan that creates and holds the target, the
restore Job's writing, the worker's verification, retries, discards and refusals; who asked
in every record; and the upload bucket's expiry."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts import EnvironmentInfo, EnvironmentRequest, EnvironmentState
from sandbox_science.contracts.workspace_checkpoint import RestorePlan
from sandbox_science.worker import checkpoint_jobs_cli, checkpoint_remote, checkpoint_restore
from sandbox_science.worker.checkpoint_capture import capture_upload
from sandbox_science.worker.checkpoint_jobs import inventory, verify
from sandbox_science.worker.checkpoint_records import (
    CheckpointRecords,
    HeldError,
    RecordError,
    begin_start,
)
from sandbox_science.worker.checkpoint_tree import describe, tree_index
from sandbox_science.worker.cli import main
from sandbox_science.worker.environments import EnvironmentRecords
from sandbox_science.worker.settings import Settings

from .test_checkpoint_jobs import (  # noqa: F401 - fixtures
    KEPT,
    UPLOAD,
    Garage,
    garage,
    planned,
    retained,
    save_environment,
    stopped,
    workspace,
)

TARGET = "e-restored"
TARGET_SANDBOX = "ss-restore-target"


def kept_checkpoint(tmp_path: Path, settings: Settings) -> tuple[str, Path]:
    """A kept checkpoint of a workspace with nested modes, a hardlink and a symlink."""
    plan = planned(settings)
    root = workspace(tmp_path / "source")
    (root / "research" / "private").mkdir(mode=0o750)
    (root / "research" / "private" / "key.txt").write_text("test-only data")
    (root / "research" / "private" / "key.txt").chmod(0o600)
    os.link(root / "research" / "notes", root / "research" / "notes-again")
    (root / "research" / "empty").write_bytes(b"")
    identity, _, _ = capture_upload(root, plan.source, UPLOAD, plan.capture)
    verify(settings, plan.checkpoint_id, identity, UPLOAD, "upload", "test-operator")
    checkpoint_remote.promote(UPLOAD, KEPT, identity)
    verify(settings, plan.checkpoint_id, identity, KEPT, "kept", "test-operator")
    return identity, root


def seeded(root: Path) -> Path:
    """A fresh workspace as OpenShell seeds it: the image's home and its sentinel."""
    root.mkdir()
    (root / ".openshell-initialized").write_bytes(b"initialized\n")
    (root / ".sandbox-science-prefix").mkdir()
    (root / ".sandbox-science-prefix" / "layer.txt").write_text("from the image")
    locked = root / "image-docs"
    locked.mkdir()
    (locked / "readme").write_text("seeded")
    locked.chmod(0o555)
    return root


class Created:
    """The worker's environment creation, as the local API would do it."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.requests: list[EnvironmentRequest] = []

    def __call__(self, settings: Settings, request: EnvironmentRequest) -> EnvironmentInfo:
        self.requests.append(request)
        save_environment(settings, request.environment_id, TARGET_SANDBOX, EnvironmentState.READY)
        records = EnvironmentRecords(settings.state_dir / "environments.sqlite3")
        try:
            info = records.get(request.environment_id)
        finally:
            records.close()
        assert info is not None
        return info


def suspended(settings: Settings, environment_id: str) -> bool:
    save_environment(settings, environment_id, TARGET_SANDBOX, EnvironmentState.STOPPED)
    return True


def restore_plan(settings: Settings, manifest: str, **options: Any) -> RestorePlan:
    return checkpoint_restore.plan(
        settings,
        manifest,
        options.pop("target", TARGET),
        KEPT,
        "test-operator",
        create=options.pop("create", None) or Created(settings),
        suspend=options.pop("suspend", suspended),
        **options,
    )


def blocked(settings: Settings, environment: str = TARGET) -> bool:
    try:
        begin_start(settings.state_dir, environment, datetime.now(UTC), TARGET_SANDBOX)
    except HeldError:
        return True
    return False


# The tree digest


def test_the_archive_and_the_disk_give_one_tree_digest(tmp_path, garage, retained, stopped):
    identity, root = kept_checkpoint(tmp_path, retained)
    source = checkpoint_jobs_source(retained, identity)
    with tree_index() as tree:
        checkpoint_remote.verify(KEPT, identity, source, tree=tree)
        expected = tree.digest()
    assert describe(root) == expected
    # Any difference in bytes, mode, an extra entry or a broken hardlink shows.
    mode = (root / "research" / "empty").stat().st_mode & 0o7777
    (root / "research" / "empty").chmod(0o640 if mode != 0o640 else 0o600)
    assert describe(root) != expected
    (root / "research" / "empty").chmod(mode)
    assert describe(root) == expected
    (root / "stray").write_text("x")
    assert describe(root) != expected
    assert describe(root, exclude=frozenset({"stray"})) == expected
    (root / "research" / "notes-again").unlink()
    (root / "research" / "notes-again").write_text("retained result")
    assert describe(root, exclude=frozenset({"stray"})) != expected


def checkpoint_jobs_source(settings: Settings, manifest: str) -> Any:
    records = CheckpointRecords(settings.state_dir)
    try:
        return records.receipt(manifest).source
    finally:
        records.close()


# The whole restore


def test_a_kept_checkpoint_is_restored_into_a_fresh_held_environment(
    tmp_path, garage, retained, stopped
):
    identity, original = kept_checkpoint(tmp_path, retained)
    create = Created(retained)
    plan = restore_plan(retained, identity, create=create)
    assert create.requests[0].project_id == "p"
    assert create.requests[0].policy == plan.source.policy
    assert plan.target.environment_id == TARGET
    assert plan.target.prefix_identity is None
    assert blocked(retained)  # held from the plan on, with no expiry
    # A checkpoint of the target meanwhile is refused.
    with pytest.raises(RecordError, match="not verified"):
        CheckpointRecords(retained.state_dir).place(
            planned_like(retained, TARGET), datetime.now(UTC), "op"
        )
    volume = seeded(tmp_path / "target")
    tree, receipt = checkpoint_restore.write(volume, plan, KEPT)
    assert receipt.members > 5
    assert describe(volume) == describe(original) == tree
    assert not (volume / checkpoint_restore.MARKER).exists()
    assert not (volume / "image-docs").exists()  # the seed is gone
    checkpoint_restore.verify(retained, plan.restore_id, tree, "test-operator")
    assert not blocked(retained)
    found = inventory(retained)
    assert [(r.restore_id, r.state) for r in found.restores] == [(plan.restore_id, "restored")]
    records = CheckpointRecords(retained.state_dir)
    try:
        journal = [(o.kind, o.phase, o.actor) for o in records.operations()]
    finally:
        records.close()
    assert journal == [
        ("restore", "intent", "test-operator"),
        ("restore", "verified", "test-operator"),
    ]


def planned_like(settings: Settings, environment: str) -> Any:
    """A checkpoint plan of another environment, for the hold records alone."""
    plan = planned_source(settings)
    return plan.model_copy(
        update={
            "checkpoint_id": "ckpt-" + "f" * 24,
            "source": plan.source.model_copy(
                update={"environment_id": environment, "sandbox": TARGET_SANDBOX}
            ),
        }
    )


def planned_source(settings: Settings) -> Any:
    records = CheckpointRecords(settings.state_dir)
    try:
        row = records.db.execute("SELECT checkpoint_id FROM holds LIMIT 1").fetchone()
        return records.hold(str(row[0])).plan
    finally:
        records.close()


def test_a_wrong_tree_fails_keeps_the_target_held_and_a_retry_finishes(
    tmp_path, garage, retained, stopped
):
    identity, original = kept_checkpoint(tmp_path, retained)
    plan = restore_plan(retained, identity)
    with pytest.raises(RecordError, match="differs"):
        checkpoint_restore.verify(retained, plan.restore_id, "0" * 64, "test-operator")
    assert blocked(retained)
    # A partial tree from an interrupted Job: the marker lets the retry empty it again.
    volume = seeded(tmp_path / "target")
    garage.unavailable.add(sorted(garage.keys(KEPT.bucket, "chunks"))[2])
    with pytest.raises(OSError, match="request failed"):
        checkpoint_restore.write(volume, plan, KEPT)
    assert (volume / checkpoint_restore.MARKER).read_text().strip() == plan.restore_id
    garage.unavailable.clear()
    again = checkpoint_restore.resume(retained, plan.restore_id, "test-operator")
    assert again == plan
    tree, _ = checkpoint_restore.write(volume, again, KEPT)
    assert tree == describe(original)
    checkpoint_restore.verify(retained, plan.restore_id, tree, "test-operator")
    assert not blocked(retained)
    with pytest.raises(RecordError, match="restored"):
        checkpoint_restore.resume(retained, plan.restore_id, "test-operator")


def test_the_job_refuses_a_volume_that_is_not_a_fresh_seed(tmp_path, garage, retained, stopped):
    identity, _ = kept_checkpoint(tmp_path, retained)
    plan = restore_plan(retained, identity)
    used = seeded(tmp_path / "used")
    (used / ".sandbox-science-prefix" / ".sandbox-science-prefix-id").write_text("prefix-x")
    with pytest.raises(ValueError, match="not a fresh workspace"):
        checkpoint_restore.write(used, plan, KEPT)
    assert (used / "image-docs" / "readme").exists()  # nothing was removed
    other = seeded(tmp_path / "other")
    (other / checkpoint_restore.MARKER).write_text("rst-" + "0" * 24 + "\n")
    with pytest.raises(ValueError, match="another restore"):
        checkpoint_restore.write(other, plan, KEPT)
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(ValueError, match="not an initialized workspace"):
        checkpoint_restore.write(bare, plan, KEPT)


def test_a_restore_needs_a_kept_checkpoint_intact_in_the_kept_bucket(
    tmp_path, garage, retained, stopped
):
    identity, _ = kept_checkpoint(tmp_path, retained)
    create = Created(retained)
    with pytest.raises(RecordError, match="no checkpoint"):
        restore_plan(retained, "a" * 64, create=create)
    chunk = next(k for b, k in garage.objects if b == KEPT.bucket and "/chunks/" in k)
    good = garage.objects[(KEPT.bucket, chunk)]
    garage.objects[(KEPT.bucket, chunk)] = b"x" * len(good)
    with pytest.raises(ValueError, match="differ"):
        restore_plan(retained, identity, create=create)
    assert create.requests == []  # refused before anything was created
    garage.objects[(KEPT.bucket, chunk)] = good
    records = CheckpointRecords(retained.state_dir)
    records.forget(identity, datetime.now(UTC), "op", volume_gone=True)
    records.close()
    with pytest.raises(RecordError, match="deleting, not kept"):
        restore_plan(retained, identity, create=create)


@pytest.mark.parametrize("state", [EnvironmentState.READY, EnvironmentState.STOPPED])
def test_a_restore_never_goes_into_an_existing_environment(
    tmp_path, garage, retained, stopped, state
):
    identity, _ = kept_checkpoint(tmp_path, retained)
    save_environment(retained, TARGET, "ss-someone-else", state)
    create = Created(retained)
    with pytest.raises(RecordError, match="exists"):
        restore_plan(retained, identity, create=create)
    with pytest.raises(RecordError, match="exists"):
        restore_plan(retained, identity, create=create, target="e")  # the source itself
    assert create.requests == []


def test_a_deleted_environment_is_created_again_for_its_restore(
    tmp_path, garage, retained, stopped
):
    """The environment a session names, lost with its volume, comes back under its ID."""
    identity, _ = kept_checkpoint(tmp_path, retained)
    save_environment(retained, "e", "ss-checkpoint-test", EnvironmentState.DELETED)
    plan = checkpoint_restore.plan(
        retained, identity, "e", KEPT, "op", create=Created(retained), suspend=suspended
    )
    assert plan.target.environment_id == plan.source.environment_id == "e"
    assert plan.target.sandbox == TARGET_SANDBOX != plan.source.sandbox


def test_a_plan_that_finds_something_ran_is_cancelled_and_holds_nothing(
    tmp_path, garage, retained, stopped
):
    identity, _ = kept_checkpoint(tmp_path, retained)

    created = Created(retained)

    def ran(settings: Settings, request: EnvironmentRequest) -> EnvironmentInfo:
        """Someone used the new environment before the plan held it."""
        info = created(settings, request)
        db = sqlite3.connect(settings.state_dir / "worker.sqlite3")
        db.execute(
            "INSERT INTO executions VALUES ('x1','op1','d',?,'s',1,'{}','legacy')",
            (request.environment_id,),
        )
        db.commit()
        db.close()
        return info

    with pytest.raises(RecordError, match="something ran"):
        restore_plan(retained, identity, create=ran)
    assert not blocked(retained)
    assert [r.state for r in inventory(retained).restores] == ["cancelled"]


def test_a_discarded_restore_deletes_its_target_and_holds_it_no_more(
    tmp_path, garage, retained, stopped
):
    identity, _ = kept_checkpoint(tmp_path, retained)
    plan = restore_plan(retained, identity)
    deleted: list[str] = []
    checkpoint_restore.discard(
        retained,
        plan.restore_id,
        "test-operator",
        "bad volume",
        delete=lambda _s, e: deleted.append(e),
    )
    assert deleted == [TARGET]
    assert not blocked(retained)
    with pytest.raises(RecordError, match="cancelled"):
        checkpoint_restore.verify(retained, plan.restore_id, "0" * 64, "op")
    # A later environment of the ID with another sandbox is never held by the old record.
    records = CheckpointRecords(retained.state_dir)
    try:
        assert records.operations()[-1].detail == {"reason": "bad volume", "deleted": True}
    finally:
        records.close()


def test_a_held_restore_target_holds_only_its_own_sandbox(tmp_path, garage, retained, stopped):
    identity, _ = kept_checkpoint(tmp_path, retained)
    restore_plan(retained, identity)
    assert blocked(retained)
    begin_start(retained.state_dir, TARGET, datetime.now(UTC), "ss-a-later-sandbox")
    with pytest.raises(HeldError, match="restored into it"):
        begin_start(retained.state_dir, TARGET, datetime.now(UTC))


# The commands


def test_the_restore_job_command_reads_its_plan_and_storage_from_its_environment(
    tmp_path, garage, retained, stopped, monkeypatch, capsys
):
    identity, original = kept_checkpoint(tmp_path, retained)
    plan = restore_plan(retained, identity)
    log = tmp_path / "termination-log"
    monkeypatch.setattr(checkpoint_jobs_cli, "TERMINATION_LOG", log)
    monkeypatch.setenv(checkpoint_jobs_cli.RESTORE_VARIABLE, plan.model_dump_json())
    monkeypatch.setenv(checkpoint_jobs_cli.STORAGE_VARIABLE, KEPT.model_dump_json())
    volume = seeded(tmp_path / "target")
    assert main(["checkpoint", "restore-write", "--workspace", str(volume)]) == 0
    line = capsys.readouterr().out.splitlines()[0]
    tree = line.split()[0].removeprefix("restore_tree=")
    assert tree == describe(original)
    assert log.read_text() == f"restore_tree={tree}\n"
    assert f"restore_id={plan.restore_id}" in line


def test_the_restore_commands_name_who_asks(tmp_path, garage, retained, stopped, capsys):
    settings = tmp_path / "worker.toml"
    settings.write_text(
        f'token_file = "{retained.token_file}"\nstate_dir = "{retained.state_dir}"\n'
        "[openshell]\nisolation_tier = 3\n"
    )
    identity, _ = kept_checkpoint(tmp_path, retained)
    plan = restore_plan(retained, identity)
    common = ["--settings", str(settings), "--by", "alice@test-host"]
    assert main(["checkpoint", "restore-plan", "--manifest", identity, *common]) == 1
    assert "names --manifest, --target and --storage" in capsys.readouterr().err
    assert (
        main(
            [
                "checkpoint",
                "restore-record",
                "--restore-id",
                plan.restore_id,
                "--phase",
                "job",
                "--detail",
                json.dumps({"job": "research-restore-x"}),
                *common,
            ]
        )
        == 0
    )
    assert main(["checkpoint", "restore-plan", "--resume", plan.restore_id, *common]) == 0
    assert RestorePlan.model_validate_json(capsys.readouterr().out.splitlines()[-1]) == plan
    assert (
        main(
            [
                "checkpoint",
                "restore-record",
                "--restore-id",
                plan.restore_id,
                "--phase",
                "failed",
                "--detail",
                json.dumps({"error": "job failed"}),
                *common,
            ]
        )
        == 0
    )
    assert blocked(retained)  # a failure keeps the target held
    records = CheckpointRecords(retained.state_dir)
    try:
        rows = [(o.phase, o.actor) for o in records.operations()]
    finally:
        records.close()
    assert rows == [
        ("intent", "test-operator"),
        ("job", "alice@test-host"),
        ("retry", "alice@test-host"),
        ("failed", "alice@test-host"),
    ]


# Who asked, in the hold records


def test_a_released_hold_records_who_released_it(tmp_path, retained, stopped):
    plan = planned(retained)
    records = CheckpointRecords(retained.state_dir)
    try:
        assert records.released_by(plan.checkpoint_id) == (None, None)
        records.release(plan.checkpoint_id, datetime.now(UTC), "not needed", "bob@test-host")
        assert records.released_by(plan.checkpoint_id) == ("bob@test-host", "not needed")
    finally:
        records.close()


def test_records_from_before_the_releasing_actor_gain_its_column(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    db = sqlite3.connect(state / "checkpoints.sqlite3")
    db.execute(
        """CREATE TABLE holds (
    checkpoint_id TEXT PRIMARY KEY, environment_id TEXT NOT NULL, sandbox TEXT NOT NULL,
    plan TEXT NOT NULL, placed_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    placed_by TEXT NOT NULL, released_at TEXT, released_reason TEXT) STRICT"""
    )
    db.commit()
    db.close()
    records = CheckpointRecords(state)
    try:
        columns = [str(r[1]) for r in records.db.execute("PRAGMA table_info(holds)")]
    finally:
        records.close()
    assert columns[-1] == "released_by"


# Upload expiry


def test_upload_expiry_keeps_what_unpromoted_checkpoints_need(
    tmp_path, garage: Garage, retained, stopped, capsys
):
    from sandbox_science.worker.checkpoint_jobs import verify as verify_checkpoint

    identity, _ = kept_checkpoint(tmp_path, retained)  # promoted: its uploads may go
    garage.now = datetime.now(UTC)
    plan = planned(retained)
    root = workspace(tmp_path / "pending")
    (root / "research" / "notes").write_text("pending result")
    pending, _, _ = capture_upload(root, plan.source, UPLOAD, plan.capture)
    verify_checkpoint(retained, plan.checkpoint_id, pending, UPLOAD, "upload", "op")
    records = CheckpointRecords(retained.state_dir)
    try:
        protected = set(records.protected_uploads())
    finally:
        records.close()
    assert pending in protected
    assert identity not in protected
    later = garage.now + timedelta(days=8)
    from sandbox_science.testing import FakeClock

    removed, kept = checkpoint_remote.expire(
        UPLOAD, timedelta(days=7), protected, clock=FakeClock(later)
    )
    assert removed > 0
    left = garage.keys(UPLOAD.bucket, "manifests") | garage.keys(UPLOAD.bucket, "chunks")
    assert left == protected
    assert garage.keys(KEPT.bucket, "manifests") == {identity}  # never the kept bucket
    # Nothing is younger than the cutoff in a second pass at the same time.
    assert checkpoint_remote.expire(
        UPLOAD, timedelta(days=7), protected, clock=FakeClock(later)
    ) == (0, kept)


def test_upload_expiry_waits_while_a_checkpoint_is_under_way(tmp_path, retained, stopped, capsys):
    settings = tmp_path / "worker.toml"
    settings.write_text(
        f'token_file = "{retained.token_file}"\nstate_dir = "{retained.state_dir}"\n'
        "[openshell]\nisolation_tier = 3\n"
    )
    command = [
        "checkpoint",
        "upload-expiry",
        "--phase",
        "intent",
        "--older-than-hours",
        "168",
        "--settings",
        str(settings),
        "--by",
        "carol@test-host",
    ]
    planned(retained)
    assert main(command) == 1
    assert "under way" in capsys.readouterr().err
    records = CheckpointRecords(retained.state_dir)
    try:
        hold = records.holds(datetime.now(UTC))[0]
        records.release(hold.checkpoint_id, datetime.now(UTC), "test", "op")
    finally:
        records.close()
    assert main(command) == 0
    records = CheckpointRecords(retained.state_dir)
    try:
        rows = [(o.kind, o.phase, o.actor, o.detail) for o in records.operations()]
    finally:
        records.close()
    assert rows == [("upload-expiry", "intent", "carol@test-host", {"older_than_hours": 168})]


def test_a_running_new_environment_is_suspended_before_the_restore(
    tmp_path, garage, retained, stopped
):
    identity, _ = kept_checkpoint(tmp_path, retained)
    stopped[:] = ["Running", "Stopped"]
    asked: list[str] = []

    def suspend(settings: Settings, environment_id: str) -> bool:
        asked.append(environment_id)
        return suspended(settings, environment_id)

    restore_plan(retained, identity, suspend=suspend)
    assert asked == [TARGET]
    assert blocked(retained)


def test_the_local_create_and_delete_use_the_workers_own_api(tmp_path, retained):
    import http.server
    import threading

    seen: list[tuple[str, str, bytes]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def respond(self, body: bytes) -> None:
            self.send_response(201 if self.command == "POST" else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            seen.append(("POST", self.path, self.rfile.read(length)))
            request = EnvironmentRequest.model_validate_json(seen[-1][2])
            info = EnvironmentInfo(
                environment_id=request.environment_id,
                project_id=request.project_id,
                profile=request.profile,
                image="example.invalid/research@sha256:" + "1" * 64,
                sandbox=TARGET_SANDBOX,
                policy=request.policy,
                state=EnvironmentState.READY,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            self.respond(info.model_dump_json().encode())

        def do_DELETE(self) -> None:
            seen.append(("DELETE", self.path, self.headers["Authorization"][:12].encode()))
            self.respond(b"{}")

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        own = retained.model_copy(update={"listen": f"127.0.0.1:{server.server_address[1]}"})
        request = EnvironmentRequest(
            environment_id=TARGET,
            project_id="p",
            profile="crypto-base",
            policy="sha256:" + "2" * 64,
        )
        assert checkpoint_restore.local_create(own, request).sandbox == TARGET_SANDBOX
        checkpoint_restore.local_delete(own, TARGET)
    finally:
        server.shutdown()
        server.server_close()
    assert [(method, path) for method, path, _ in seen] == [
        ("POST", "/v1/environments"),
        ("DELETE", f"/v1/environments/{TARGET}"),
    ]
    assert seen[1][2] == b"Bearer test-"


def test_upload_expiry_command_empties_a_bucket_of_stale_leftovers(
    tmp_path, garage: Garage, retained, stopped, capsys
):
    """A qualification's leftovers: promoted or abandoned uploads, no checkpoint under way."""
    kept_checkpoint(tmp_path, retained)
    plan = planned(retained)  # a capture that never finished: an upload with no record
    capture_upload(workspace(tmp_path / "abandoned"), plan.source, UPLOAD, plan.capture)
    records = CheckpointRecords(retained.state_dir)
    try:
        records.release(plan.checkpoint_id, datetime.now(UTC), "abandoned", "op")
    finally:
        records.close()
    garage.now = datetime.now(UTC) + timedelta(days=30)  # nothing new is written meanwhile
    for key in list(garage.written):
        garage.written[key] = datetime.now(UTC) - timedelta(days=30)
    settings = tmp_path / "worker.toml"
    settings.write_text(
        f'token_file = "{retained.token_file}"\nstate_dir = "{retained.state_dir}"\n'
        "[openshell]\nisolation_tier = 3\n"
    )
    intent = ["checkpoint", "upload-expiry", "--older-than-hours", "168"]
    common = ["--settings", str(settings), "--by", "op"]
    assert main([*intent, "--phase", "intent", *common]) == 0
    protected = tmp_path / "protected.txt"
    protected.write_text(capsys.readouterr().out)
    assert protected.read_text() == ""  # every checkpoint was promoted
    storage = tmp_path / "upload.toml"
    storage.write_text(
        "\n".join(f'{key} = "{value}"' for key, value in UPLOAD.model_dump().items()) + "\n"
    )
    storage.chmod(0o600)
    expire = ["checkpoint", "expire-objects", "--storage", str(storage)]
    assert main([*expire, "--older-than-hours", "168", "--protected-file", str(protected)]) == 0
    assert capsys.readouterr().out.startswith("expired=")
    assert not garage.keys(UPLOAD.bucket, "chunks")
    assert not garage.keys(UPLOAD.bucket, "manifests")
    assert garage.keys(KEPT.bucket, "manifests")  # the kept bucket is never touched
    assert main([*intent, "--phase", "done", *common]) == 0
