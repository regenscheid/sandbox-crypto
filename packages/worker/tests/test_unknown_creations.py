# SPDX-License-Identifier: MIT
"""Creations with an unknown outcome are reconciled on confirmed absence (B3).

Against the fake ``openshell`` of ``test_environments``: a creation whose response was
lost before any identity appeared stays ``creating`` until two gateway reads, at least
``creation_recheck`` apart and past ``creation_grace``, find no trace of its sandbox.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sandbox_science.contracts import EnvironmentState, ErrorKind
from sandbox_science.testing import FakeClock, eventually
from sandbox_science.worker.cli import main
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.state_lock import StateLock
from sandbox_science.worker.unknown_creations import CREATION_ROLLED_BACK, CreationReconciler

from .test_environments import IMAGE, TARGETS, Setup

pytestmark = pytest.mark.slow

GRACE = 420.0
RECHECK = 30.0


@pytest.fixture
def paused(tmp_path: Path) -> Setup:
    return Setup(tmp_path, clock=FakeClock(datetime.now(UTC)))


async def lost(setup: Setup, environment_id: str = "env-1") -> None:
    """A creation whose response is lost before the gateway shows any sandbox."""
    setup.fake.update(fail_create=True)
    with pytest.raises(WorkerError, match="could not start"):
        await setup.environments.create(setup.request(environment_id))
    setup.fake.update(fail_create=False)
    info = setup.environments.get(environment_id)
    assert info.state is EnvironmentState.CREATING
    assert info.sandbox_id is None


def no_sandbox_touched(setup: Setup) -> None:
    for command in ("delete", "stop", "start"):
        assert setup.fake.calls("sandbox", command) == []


async def test_one_read_past_the_grace_stays_inconclusive(paused: Setup) -> None:
    await lost(paused)
    original = paused.environments.get("env-1")
    with pytest.raises(WorkerError, match="younger than the creation grace") as young:
        await paused.environments.delete("env-1")
    assert young.value.kind is ErrorKind.UNAVAILABLE
    assert "creation outcome is unknown" in young.value.message
    assert paused.fake.calls("sandbox", "list") == []
    assert paused.environments.records.observations(original) == ()

    paused.fake_clock().advance(GRACE + 1)
    with pytest.raises(WorkerError, match=r"seen absent once, at .*a second read after"):
        await paused.environments.delete("env-1")
    assert paused.environments.get("env-1") == original
    assert "env-1" in paused.environments.records.live(["p1"])
    [seen] = paused.environments.records.observations(original)
    assert seen.observed_at == paused.fake_clock().now()
    assert seen.listed == 0
    # A second read too soon confirms nothing either.
    paused.fake_clock().advance(RECHECK - 1)
    result = await paused.environments.reconcile_creation("env-1")
    assert result.action == "inconclusive"
    assert "seen absent 2 times since" in result.message
    assert result.retry_at == seen.observed_at + timedelta(seconds=RECHECK)
    assert paused.environments.get("env-1").state is EnvironmentState.CREATING
    no_sandbox_touched(paused)


async def test_two_reads_past_the_grace_confirm_absence(paused: Setup) -> None:
    other, _ = await paused.environments.create(paused.request("env-2"))
    await lost(paused)
    original = paused.environments.get("env-1")
    paused.fake_clock().advance(GRACE + 1)
    first = await paused.environments.reconcile_creation("env-1")
    assert first.action == "inconclusive"
    paused.fake_clock().advance(RECHECK)
    second = await paused.environments.reconcile_creation("env-1")
    assert second.action == "rolled_back"
    assert CREATION_ROLLED_BACK in second.message
    deleted = paused.environments.get("env-1")
    assert deleted.state is EnvironmentState.DELETED
    records = paused.environments.records
    assert records.deletion_confirmed(original)
    reconciled = records.reconciliation("env-1")
    assert reconciled is not None
    assert reconciled[:2] == (original.sandbox, CREATION_ROLLED_BACK)
    assert reconciled[3] == 2
    assert records.observations(original) == ()  # forgotten with the record's other reads
    assert "env-1" not in records.live(["p1"])
    no_sandbox_touched(paused)
    assert other.sandbox in paused.fake.read()["sandboxes"]
    # The ID is free again, under a new sandbox name.
    again, created = await paused.environments.create(paused.request())
    assert created
    assert again.state is EnvironmentState.READY
    assert again.sandbox != original.sandbox


@pytest.mark.parametrize("where", ["get", "list"])
async def test_a_sandbox_that_appears_between_reads_is_adopted_never_deleted(
    paused: Setup, where: str
) -> None:
    await lost(paused)
    original = paused.environments.get("env-1")
    paused.fake_clock().advance(GRACE + 1)
    assert (await paused.environments.reconcile_creation("env-1")).action == "inconclusive"
    # The disconnected upstream creation completes after the first read.
    if where == "get":
        paused.fake.update(sandboxes={original.sandbox: {"id": "late-id", "phase": "Ready"}})
    else:  # listed only, as a listing may run ahead of the name's lookup
        paused.fake.update(extra_listed=[{"id": "late-id", "name": original.sandbox}])
    paused.fake_clock().advance(RECHECK)
    result = await paused.environments.reconcile_creation("env-1")
    assert result.action == "adopted"
    adopted = paused.environments.get("env-1")
    assert adopted.state is EnvironmentState.CREATING
    assert adopted.sandbox_id == "late-id"
    no_sandbox_touched(paused)
    assert not paused.environments.records.deletion_confirmed(adopted)
    again = await paused.environments.reconcile_creation("env-1")
    assert again.action == "skipped"
    assert "deletes it by identity" in again.message


@pytest.mark.parametrize("problem", ["paged", "labels", "nameless"])
async def test_a_listing_that_cannot_show_absence_confirms_nothing(
    paused: Setup, problem: str
) -> None:
    await lost(paused)
    original = paused.environments.get("env-1")
    if problem == "paged":
        paused.fake.update(page_token="next")
        reason = "more pages"
    elif problem == "labels":
        other = {"id": "x", "name": "ss-other", "labels": {"owner": {"env": "env-1"}}}
        paused.fake.update(extra_listed=[other])
        reason = "labels or metadata naming this environment"
    else:
        paused.fake.update(extra_listed=[{"name": original.sandbox}])
        reason = "without an identity"
    for _ in range(3):
        paused.fake_clock().advance(GRACE)
        result = await paused.environments.reconcile_creation("env-1")
        assert result.action == "inconclusive"
        assert reason in result.message
    assert paused.environments.records.observations(original) == ()
    assert paused.environments.get("env-1").state is EnvironmentState.CREATING


async def test_gateway_failures_count_as_no_read(paused: Setup) -> None:
    await lost(paused)
    paused.fake_clock().advance(GRACE + 1)
    paused.fake.update(fail_list=True)
    with pytest.raises(WorkerError, match="synthetic gateway unavailable"):
        await paused.environments.reconcile_creation("env-1")
    assert paused.environments.records.observations(paused.environments.get("env-1")) == ()


async def test_delete_succeeds_by_the_same_rule(paused: Setup) -> None:
    await lost(paused)
    paused.fake_clock().advance(GRACE + 1)
    with pytest.raises(WorkerError, match="seen absent once"):
        await paused.environments.delete("env-1")
    paused.fake_clock().advance(RECHECK)
    deleted = await paused.environments.delete("env-1")
    assert deleted.state is EnvironmentState.DELETED
    reconciled = paused.environments.records.reconciliation("env-1")
    assert reconciled is not None
    assert reconciled[1] == CREATION_ROLLED_BACK
    no_sandbox_touched(paused)


async def test_a_retried_creation_replaces_a_confirmed_absence(paused: Setup) -> None:
    await lost(paused)
    original = paused.environments.get("env-1")
    paused.fake_clock().advance(GRACE + 1)
    with pytest.raises(WorkerError, match="seen absent once"):
        await paused.environments.create(paused.request())
    paused.fake_clock().advance(RECHECK)
    replacement, created = await paused.environments.create(paused.request())
    assert created
    assert replacement.sandbox != original.sandbox
    assert paused.fake.calls("sandbox", "delete") == []


async def test_the_automatic_pass_clears_unknown_outcomes(paused: Setup) -> None:
    clock = paused.fake_clock()
    ready, _ = await paused.environments.create(paused.request("env-ready"))
    await lost(paused)
    reconciler = CreationReconciler(paused.environments, RECHECK, clock=clock)
    task = asyncio.create_task(reconciler.run())
    try:
        # Younger than the grace: the passes make no read.
        for _ in range(int(GRACE // RECHECK) - 1):
            await eventually(lambda: clock.sleepers == 1)
            clock.advance(RECHECK)
        await eventually(lambda: clock.sleepers == 1)
        assert paused.fake.calls("sandbox", "list") == []
        clock.advance(2 * RECHECK)  # past the grace: the first read
        await eventually(lambda: len(paused.fake.calls("sandbox", "list")) == 1)
        await eventually(lambda: clock.sleepers == 1)
        assert paused.environments.get("env-1").state is EnvironmentState.CREATING
        clock.advance(RECHECK)  # the second
        await eventually(lambda: paused.environments.get("env-1").state is EnvironmentState.DELETED)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert paused.environments.get("env-ready") == ready
    no_sandbox_touched(paused)


async def test_the_pass_skips_young_records_identities_and_busy_environments(
    paused: Setup,
) -> None:
    await lost(paused)
    await lost(paused, "env-2")
    paused.fake_clock().advance(GRACE + 1)
    await lost(paused, "env-young")
    paused.fake.update(fail_create_after=True, fail_delete=True)
    with pytest.raises(WorkerError, match="creation response lost"):
        await paused.environments.create(paused.request("env-identity"))
    paused.fake.update(fail_create_after=False, fail_delete=False)
    assert paused.environments.get("env-identity").sandbox_id is not None
    # A deletion of env-2 under way holds its lock; the pass leaves env-2 to it.
    assert (await paused.environments.reconcile_creation("env-2")).action == "inconclusive"
    paused.fake_clock().advance(RECHECK)
    holding, go = asyncio.Event(), asyncio.Event()

    async def hold(environment_id: str) -> None:
        if environment_id == "env-2":
            holding.set()
            await go.wait()

    paused.environments.required_deleting.append(hold)
    deletion = asyncio.create_task(paused.environments.delete("env-2"))
    await holding.wait()
    results = await paused.environments.reconcile_creations()
    go.set()
    assert (await deletion).state is EnvironmentState.DELETED
    assert [(r.environment_id, r.action) for r in results] == [("env-1", "inconclusive")]
    assert paused.environments.get("env-young").state is EnvironmentState.CREATING


# The operator command.


def settings_file(setup: Setup, recheck: float = RECHECK) -> Path:
    path = setup.tmp / "worker.toml"
    path.write_text(
        f'token_file = "worker.token"\nstate_dir = "{setup.state_dir}"\n\n'
        f'[openshell]\nisolation_tier = 3\ncli = "{setup.fake.cli}"\n'
        f'env = {{FAKE_OPENSHELL_STATE = "{setup.fake.path}", PATH = "{os.environ["PATH"]}"}}\n\n'
        "[openshell.environments]\n"
        f'policy_store = "{setup.tmp / "middleware"}"\ntargets = "{TARGETS}"\n'
        f'ceiling = "{setup.tmp / "pool-ceiling.yaml"}"\nprofiles = {{minimal = "{IMAGE}"}}\n'
        f"creation_recheck = {recheck}\n"
    )
    return path


def lost_long_ago(setup: Setup, environment_id: str = "env-1", age: float = GRACE + 60) -> None:
    """A creation with an unknown outcome, recorded ``age`` seconds ago."""
    asyncio.run(lost(setup, environment_id))
    info = setup.environments.get(environment_id)
    old = datetime.now(UTC) - timedelta(seconds=age)
    setup.environments.records.save(info.model_copy(update={"created_at": old, "updated_at": old}))


def test_the_command_lists_creating_environments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = Setup(tmp_path)
    asyncio.run(setup.environments.create(setup.request("env-ready")))
    lost_long_ago(setup)
    settings = settings_file(setup)
    assert main(["environments", "list", "--settings", str(settings), "--state", "creating"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1
    assert out[0].startswith("env-1 creating project p1 session sandbox ss-")
    assert "(no identity)" in out[0]
    assert out[0].endswith("seen absent 0 times")
    assert main(["environments", "list", "--settings", str(settings)]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_the_command_refuses_without_waiting_then_confirms(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = Setup(tmp_path)
    lost_long_ago(setup)
    settings = str(settings_file(setup))
    assert main(["environments", "reconcile", "--settings", settings, "--no-wait", "env-1"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("inconclusive: sandbox creation outcome is unknown")
    assert "seen absent once, at " in out
    assert "a second read after " in out
    assert setup.environments.get("env-1").state is EnvironmentState.CREATING
    assert main(["environments", "list", "--settings", settings, "--state", "creating"]) == 0
    assert "seen absent 1 times, last at " in capsys.readouterr().out


def test_the_command_waits_for_the_second_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = Setup(tmp_path)
    lost_long_ago(setup)
    settings = str(settings_file(setup, recheck=1))
    assert main(["environments", "reconcile", "--settings", settings, "env-1"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("waiting: ")
    assert "seen absent once" in out[0]
    assert out[1].startswith("rolled_back: ")
    assert f"env-1 is deleted ({CREATION_ROLLED_BACK})" in out[1]
    assert setup.environments.get("env-1").state is EnvironmentState.DELETED
    no_sandbox_touched(setup)


def test_the_command_adopts_a_sandbox_that_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = Setup(tmp_path)
    lost_long_ago(setup)
    sandbox = setup.environments.get("env-1").sandbox
    setup.fake.update(sandboxes={sandbox: {"id": "late-id", "phase": "Ready"}})
    assert (
        main(["environments", "reconcile", "--settings", str(settings_file(setup)), "env-1"]) == 0
    )
    out = capsys.readouterr().out
    assert out.startswith(f"adopted: sandbox {sandbox} exists with identity late-id")
    assert setup.environments.get("env-1").sandbox_id == "late-id"
    no_sandbox_touched(setup)


def test_the_command_refuses_a_young_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = Setup(tmp_path)
    lost_long_ago(setup, age=60)
    assert (
        main(["environments", "reconcile", "--settings", str(settings_file(setup)), "env-1"]) == 1
    )
    out = capsys.readouterr().out
    assert out.startswith("inconclusive: ")
    assert "younger than the creation grace (420 s)" in out
    assert setup.fake.calls("sandbox", "list") == []


def test_the_command_refuses_while_the_worker_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = Setup(tmp_path)
    lost_long_ago(setup)
    with StateLock(setup.state_dir):
        code = main(["environments", "reconcile", "--settings", str(settings_file(setup)), "env-1"])
    assert code == 1
    error = capsys.readouterr().err
    assert "another worker uses the state directory" in error
    assert "reconciles creating environments itself every 30 s" in error
    assert setup.environments.get("env-1").state is EnvironmentState.CREATING


def test_the_command_reports_other_states(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = Setup(tmp_path)
    asyncio.run(setup.environments.create(setup.request()))
    settings = str(settings_file(setup))
    assert main(["environments", "reconcile", "--settings", settings, "env-1"]) == 1
    assert capsys.readouterr().out == "skipped: env-1 is ready, not creating\n"
    assert main(["environments", "reconcile", "--settings", settings, "nope"]) == 1
    assert "no environment 'nope'" in capsys.readouterr().err
