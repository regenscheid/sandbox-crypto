# SPDX-License-Identifier: MIT
"""Why an executor offers no Python kernel: the executor's reason and the worker's bounding."""

from __future__ import annotations

import importlib.util
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts import Capabilities
from sandbox_science.worker import connections, executor

from .harness import Harness


def _without(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    find_spec = importlib.util.find_spec

    def hidden(name: str, *args: Any) -> Any:
        return None if name == missing else find_spec(name, *args)

    monkeypatch.setattr(importlib.util, "find_spec", hidden)


def test_missing_jupyter_client_is_named_in_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _without(monkeypatch, "jupyter_client")
    languages, reason = executor.available_languages()
    assert languages == []
    assert reason is not None
    assert reason.startswith("jupyter_client is not importable by ")


def test_missing_ipykernel_is_named_in_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _without(monkeypatch, "ipykernel")
    assert executor.available_languages()[1] == f"ipykernel is not importable by {sys.executable}"


def test_a_missing_kernelspec_and_a_broken_listing_are_explained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jupyter_client.kernelspec import KernelSpecManager

    monkeypatch.setattr(KernelSpecManager, "find_kernel_specs", lambda self: {"julia-1.10": "/x"})
    languages, reason = executor.available_languages()
    assert languages == []
    assert reason == "no kernelspec for python3 (found: julia-1.10)"

    def broken(self: KernelSpecManager) -> dict[str, str]:
        raise RuntimeError("bad\nkernels\x00" + "x" * 1000)

    monkeypatch.setattr(KernelSpecManager, "find_kernel_specs", broken)
    languages, reason = executor.available_languages()
    assert languages == []
    assert reason is not None
    assert reason.startswith("jupyter_client cannot list kernelspecs: RuntimeError(")
    assert len(reason) == executor.MAX_KERNEL_REASON
    assert reason.isprintable()


def test_a_python_kernel_needs_no_reason() -> None:
    languages, reason = executor.available_languages()
    assert "python" in languages
    assert reason is None


def test_the_worker_keeps_one_bounded_printable_reason() -> None:
    hello: dict[str, Any] = {
        "location": {"hostname": "box"},
        "languages": [],
        "kernel_reason": "bad\x1b[31m" + "y" * 1000,
    }
    offered = connections.describe("env-1", hello)
    assert offered.kernel_reason is not None
    assert len(offered.kernel_reason) <= connections.MAX_KERNEL_REASON
    assert offered.kernel_reason.isprintable()
    for value in (None, 7, ["x"], ""):
        assert (
            connections.describe("env-1", {**hello, "kernel_reason": value}).kernel_reason is None
        )


@pytest.mark.asyncio
@pytest.mark.slow
async def test_capabilities_report_why_no_kernel_was_found(tmp_path: Path) -> None:
    # -S leaves the interpreter without its site-packages, as a system python3 is without
    # ipykernel; the real executor then says so in its hello.
    bare = tmp_path / "bare-python"
    bare.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n')
    bare.chmod(bare.stat().st_mode | stat.S_IXUSR)
    started = await Harness.start(tmp_path / "state", python=str(bare))
    try:
        _, body = await started.get("/v1/capabilities?environment_id=env1")
        capabilities = Capabilities.model_validate(body)
        assert capabilities.languages == ()
        assert capabilities.kernel_reason is not None
        assert "jupyter_client is not importable" in capabilities.kernel_reason
    finally:
        await started.close()
