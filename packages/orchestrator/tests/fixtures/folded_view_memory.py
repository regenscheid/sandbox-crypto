# SPDX-License-Identifier: MIT
"""Fresh-process measurement of rebuilding a fixed view over increasing folded output."""

from __future__ import annotations

import gc
import hashlib
import json
import sys
import tracemalloc
from contextlib import closing
from pathlib import Path

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.history import Archive, Entry, FoldRecord, Geometry, session_view
from sandbox_science.orchestrator.history.record import FORMAT
from sandbox_science.orchestrator.projects import ProjectStore

ROOT = ExecutionContext(project_id="p", session_id="s", environment_id="e")
BYTES = 512 * 1024


def payload(index: int, count: int) -> str:
    return json.dumps(
        {
            "state": "succeeded" if index == count - 1 else "failed",
            "value": index,
            "content": "a" * BYTES,
        }
    )


def prepare(path: Path, count: int) -> None:
    with closing(ProjectStore(path)) as projects:
        projects.create("p", "minimal", "sha256:" + "a" * 64)
        projects.open_session("p", "s", dedicated=False)
        archive = Archive(projects.control)
        first = last = None
        chosen = []
        for index in range(count):
            call = {
                "id": f"call-{index}",
                "type": "function",
                "function": {"name": "history_search", "arguments": "{}"},
            }
            archive.append(
                "p",
                "s",
                "r",
                Entry(
                    "assistant",
                    "assistant",
                    None,
                    tool_calls=(call,),
                    native=b"b" * BYTES,
                    native_format="fixture",
                ),
            )
            row = archive.append(
                "p",
                "s",
                "r",
                Entry(
                    "tool",
                    "tool_result",
                    payload(index, count),
                    source="history_search",
                    call_id=f"call-{index}",
                ),
            )
            if first is None:
                first = row
            last = row
            if index in (0, count // 2, count - 1):
                chosen.append((row.message_id, index))
        assert first is not None
        assert last is not None
        record = FoldRecord(
            level=1,
            first=(1, first.message_id),
            last=(last.position, last.message_id),
            runs=("r",),
            dates=(first.created, last.created),
            skeleton=("Old computations",),
            markers=(
                {
                    "kind": "failures",
                    "subject": "history_search",
                    "count": 1,
                    "ids": [first.message_id],
                },
            ),
            keys=(),
        )
        folded = archive.append(
            "p",
            "s",
            "r",
            Entry(
                "user", "fold", record.text("fixture"), native=record.stored(), native_format=FORMAT
            ),
        )
        archive.append("p", "s", "r", Entry("user", "task", "Continue the research."))
        archive.append("p", "s", "r", Entry("assistant", "assistant", "The raw tail."))
        # A large child and a previous state block must not enter the parent view.
        archive.append(
            "p", "s", "child", Entry("assistant", "assistant", "c" * BYTES * count), parent="r"
        )
        archive.append("p", "s", "r", Entry("user", "state_block", "d" * BYTES * count))
        (path / "fixture.json").write_text(
            json.dumps(
                {
                    "chosen": chosen,
                    "fold": folded.message_id,
                    "fixed": last.message_id,
                    "count": count,
                }
            )
        )


def measure(path: Path, inline: bool) -> None:
    pins = json.loads((path / "fixture.json").read_text())
    with closing(ProjectStore(path)) as projects:
        archive = Archive(projects.control)
        gc.collect()

        # getrusage's high-water mark survives exec from a large pytest worker.
        # /proc's VmHWM belongs to this freshly exec'd process's address space.
        def rss() -> int:
            lines = Path("/proc/self/status").read_text().splitlines()
            return next(int(line.split()[1]) * 1024 for line in lines if line.startswith("VmHWM:"))

        baseline = rss()
        tracemalloc.start()
        if inline:
            rows = archive.messages("p", "s")
            assert len(rows) > pins["count"]
            size = sum(len(row.entry.content or "") for row in rows)
        else:
            view = session_view(
                archive, ROOT, geometry=Geometry(ceiling=100_000, prompt=0, chunk=10_000, scale=1)
            )
            assert view.retired[pins["fold"]][0].endswith(f"({pins['fixed']})")
            assert [part.kind for part in view.parts] == ["task", "assistant"]
            assert view.folded == (1, pins["count"] * 2)
            size = len(json.dumps(view.messages))
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        growth = max(0, rss() - baseline)
        for message, index in pins["chosen"]:
            found = archive.message("p", message).entry.content
            assert found is not None
            assert (
                hashlib.sha256(found.encode()).digest()
                == hashlib.sha256(payload(index, pins["count"]).encode()).digest()
            )
        print(json.dumps({"python_peak": peak, "rss_growth": growth, "view_bytes": size}))


if __name__ == "__main__":
    mode, root, count = sys.argv[1:]
    if mode == "prepare":
        prepare(Path(root), int(count))
    else:
        measure(Path(root), mode == "inline")
