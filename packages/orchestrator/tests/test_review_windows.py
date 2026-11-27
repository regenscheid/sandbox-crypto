# SPDX-License-Identifier: MIT
"""Reviews keep up with work that piles up, and the summary says what was reviewed
(CR-49 as amended)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from sandbox_science.contracts.artifact_inventory import ArtifactPublicationSource
from sandbox_science.orchestrator.consistency.artifacts import ingest_artifacts
from sandbox_science.orchestrator.consistency.models import ReviewConfig
from sandbox_science.orchestrator.consistency.runner import ReviewerRunner
from sandbox_science.orchestrator.consistency.store import window_config

from .test_artifact_review import build, dispatch, publish
from .test_consistency import MAIN, claim
from .test_consistency import dispatch as dispatch_review
from .test_consistency import store as store
from .test_consistency_budget import claims
from .test_review_limits import LabReviewer


def test_a_window_grows_with_its_backlog_and_its_budget_with_it() -> None:
    """Up to twice the configured window; calls, time, context, bytes and a token cap in
    proportion, within their ceilings; a backlog that fits changes nothing."""
    base = ReviewConfig(token_budget=400_000)
    assert window_config(base, 10) == base
    assert window_config(base, 16) == base
    grown = window_config(base, 24)
    assert (grown.max_feed_items, grown.max_turns, grown.time_seconds) == (24, 36, 450)
    assert (grown.max_input_chars, grown.max_feed_bytes) == (450_000, 384 * 1024)
    assert grown.token_budget == 600_000
    widest = window_config(base, 900)
    assert (widest.max_feed_items, widest.max_turns, widest.time_seconds) == (32, 48, 600)
    assert widest.max_input_chars == 500_000
    # Stored as the review's settings, it loads again.
    assert ReviewConfig.model_validate_json(widest.model_dump_json()) == widest


async def test_a_backlog_is_reviewed_in_one_grown_window_not_skipped(store) -> None:
    """Twenty-four new items, over the configured 16, are one checkpoint's window, which
    reads them all with the grown budget; nothing is recorded unreviewed."""
    reviews, _, _ = store
    seqs = claims(reviews, 24)
    review = dispatch_review(reviews, "checkpoint")
    run = reviews.run("p", "s", review)
    assert (run["start_seq"], run["end_seq"]) == (seqs[0], seqs[-1])
    assert run["config"]["max_feed_items"] == 24
    await ReviewerRunner(reviews, "p", "s", review, "owner", LabReviewer()).run()
    assert reviews.run("p", "s", review)["state"] == "passed"
    coverage = reviews.coverage("p", "s")
    assert coverage["complete"]
    assert coverage["items"] == {
        "total": 24,
        "reviewed": 24,
        "read_in_part": 0,
        "not_read": 0,
        "unreviewed": 0,
        "not_recorded": 0,
    }
    assert reviews.listing("p", "s")["unreviewed"] == []


def test_a_wider_backlog_takes_the_widest_window_and_the_next_continues(store) -> None:
    reviews, _, now = store
    seqs = claims(reviews, 40)
    review = dispatch_review(reviews, "checkpoint")
    run = reviews.run("p", "s", review)
    assert (run["start_seq"], run["end_seq"]) == (seqs[0], seqs[31])
    reviews.finish("p", "s", review, "owner", "passed", "Checked.")
    now[0] += 200
    claim(reviews, "Another step.", "later")
    following = reviews.run("p", "s", dispatch_review(reviews, "checkpoint"))
    assert following["start_seq"] == seqs[32]
    assert following["config"]["max_feed_items"] == 16  # nine fit the configured window


def _recorded(source: ArtifactPublicationSource, **update: object) -> ArtifactPublicationSource:
    return source.model_copy(update={"kind": "execution_record", "execution_id": "exe-1", **update})


def test_a_deleted_working_file_is_nothing_to_read_and_a_used_one_stays_unread(
    tmp_path: Path,
) -> None:
    """A file the session's main run's commands made and deleted, named nowhere that
    relies on it, is counted apart as nothing to read; one published before, one a
    delegated run's job wrote, one the final answer names by path and one a document
    names stay unread, with why."""
    state = build(tmp_path)
    reviews, inventory, source = state
    recorded = _recorded(source)
    child = _recorded(source, role="delegated", parent_run="root", delegation="dlg-1")
    try:
        for path, by in (
            ("scratch.txt", recorded),
            ("child.txt", child),
            ("named.txt", recorded),
            ("cited.txt", recorded),
        ):
            sha = hashlib.sha256(path.encode()).hexdigest()
            inventory.record_metadata(
                by, path, sha256=sha, size=9, media_type="text/plain", note="quota"
            )
            inventory.record_deletion(by, path)
        publish(state, path="report.txt")
        inventory.record_deletion(recorded, "report.txt")
        reviews.documents.create(
            "p", name="notes", title="N", text="See cited.txt.\n", author=MAIN, session_id="s"
        )
        ingest_artifacts(reviews, "p", "s", drain=True)
        review = dispatch(reviews, "checkpoint")
        reviews.finish("p", "s", review, "owner", "passed", "Checked.")
        summary = reviews.final_summary("p", "s", review, answer="The table is in named.txt.")
        deleted = {u["path"] for u in summary["unread"] if u["why"] == "the file was deleted"}
        assert deleted == {
            "sessions/s/child.txt",
            "sessions/s/named.txt",
            "sessions/s/cited.txt",
            "sessions/s/report.txt",
        }
        assert summary["working_files_deleted"] == 1
    finally:
        reviews.documents.close()


def test_a_large_configured_window_grows_within_the_settings_bound(store) -> None:
    """A project whose window is over half the settings' bound grows only to that bound,
    and the grown settings load, so its checkpoints do not fail and retry for ever."""
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=200)
    grown = window_config(reviews.defaults, 1000)
    assert grown.max_feed_items == 256
    claims(reviews, 300)
    review = dispatch_review(reviews, "checkpoint")
    runner = ReviewerRunner(reviews, "p", "s", review, "owner", LabReviewer())
    assert runner.config.max_feed_items == 256


async def test_a_grown_window_that_covers_nothing_is_retried_smaller(store) -> None:
    """A grown checkpoint that ends without a verdict leaves its watermark and waits for
    its retry, which is not grown but shrinks (CR-31 as amended), so nothing is skipped."""
    reviews, _, _ = store
    seqs = claims(reviews, 30)
    review = dispatch_review(reviews, "checkpoint")
    assert reviews.run("p", "s", review)["config"]["max_feed_items"] == 30
    reviews.finish("p", "s", review, "owner", "capped", "Ran out.", cap_reached=True)
    progress = reviews.progress("p", "s")
    assert (progress["watermark"], progress["failures"]) == (0, 1)
    retry = reviews.run("p", "s", dispatch_review(reviews, "checkpoint"))
    assert retry["config"]["max_feed_items"] == 16
    assert (retry["start_seq"], retry["end_seq"]) == (seqs[0], seqs[3])
    assert reviews.listing("p", "s")["unreviewed"] == []


def test_counts_leave_out_ranges_of_actions_and_count_items_by_how_they_were_read(
    store,
) -> None:
    """A range of actions alone is no unreviewed work; an item a review covered but cut
    short counts as read in part, not as reviewed."""
    reviews, _, _ = store
    claim(reviews, "The order is 8.", "c1")
    reviews.observe(
        "p", "s", "long", "transcript", {"run": "author-1", "message": 2},
        {"role": "assistant", "content": "…"}, complete=False,
    )  # fmt: skip
    review = dispatch_review(reviews, "checkpoint")
    reviews.finish("p", "s", review, "owner", "passed", "Checked.")
    reviews.observe("p", "s", "act", "action", {"run": "author-1", "message": 3}, {"tool": "x"})
    coverage = reviews.coverage("p", "s")
    assert coverage["unreviewed"] == []
    assert coverage["complete"]
    assert coverage["items"] == {
        "total": 2,
        "reviewed": 1,
        "read_in_part": 1,
        "not_read": 0,
        "unreviewed": 0,
        "not_recorded": 0,
    }
