# SPDX-License-Identifier: MIT
"""Real process ownership and recovery after a fully installed upload loses its owner."""

from __future__ import annotations

import asyncio
import sys

import pytest

from sandbox_science.orchestrator.documents import DocumentError
from sandbox_science.orchestrator.uploads import UserUploads

from .test_uploads import prepared

pytestmark = pytest.mark.slow

SCRIPT = """
import os
import sys
from pathlib import Path
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.uploads import UserUploads

documents = DocumentStore(Path(sys.argv[1]))
uploads = UserUploads(documents)
if sys.argv[2] == 'crash':
    uploads.finish = lambda *args: os._exit(47)
with uploads.begin('p1','s1','same','x.bin','application/octet-stream',8) as writer:
    writer.write(b'original')
    print('owned', flush=True)
    if sys.argv[2] == 'live':
        sys.stdin.readline()
    writer.commit()
documents.close()
"""


@pytest.mark.parametrize("mode", ["live", "crash"])
async def test_cross_process_owner_is_fenced_and_drained_crash_reuses_same_reservation(
    tmp_path, mode
):
    documents = prepared(tmp_path)
    store = UserUploads(documents)
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        SCRIPT,
        str(documents.path),
        mode,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert child.stdout is not None
        assert await asyncio.wait_for(child.stdout.readline(), 5) == b"owned\n"
        if mode == "live":
            with pytest.raises(DocumentError, match="owns"):
                store.create("p1", "s1", "same", "x.bin", "application/octet-stream", b"original")
            assert child.stdin is not None
            child.stdin.write(b"commit\n")
            await child.stdin.drain()
        _, error = await asyncio.wait_for(child.communicate(), 5)
        assert child.returncode == (0 if mode == "live" else 47), error.decode()
        with documents.control.read() as tx:
            before = tx.sql.execute(
                "SELECT identity,bytes,sha256 FROM user_upload_transfers"
            ).fetchall()
        if mode == "crash":
            assert len(before) == 1
            with store.begin("p1", "s1", "same", "x.bin", "application/octet-stream", 8) as retry:
                with documents.control.read() as tx:
                    assert (
                        tx.sql.execute(
                            "SELECT identity,bytes,sha256 FROM user_upload_transfers"
                        ).fetchall()
                        == before
                    )
                retry.write(b"original")
                assert retry.commit().size == 8
        assert store.content("p1", "s1", "same") == b"original"
        with documents.control.read() as tx:
            assert tx.sql.execute("SELECT count(*) FROM user_upload_transfers").fetchone()[0] == 0
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        documents.close()
