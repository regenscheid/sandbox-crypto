# SPDX-License-Identifier: MIT
"""100 MiB uploads through HTTP, the daemon bridge, executor and shared project copies."""

import base64
import hashlib
from pathlib import Path

import pytest

from .harness import Harness

pytestmark = pytest.mark.slow


async def test_100_mib_upload_reaches_another_environment_through_daemon(tmp_path: Path) -> None:
    size = 100 * 1024 * 1024
    content = b"u" * size
    sha = hashlib.sha256(content).hexdigest()
    async with await Harness.start(tmp_path, daemon=True, shared_bytes=32 * 1024 * 1024) as worker:
        payload = {
            "context": worker.context | {"actor": "user"},
            "path": "shared/uploads/s1/large/source.pdf",
            "content_b64": base64.b64encode(content).decode(),
            "create_only": True,
        }
        status, result = await worker.post("/v1/files/write", payload)
        assert status == 200, result
        assert result == {"size": size, "sha256": sha}
        status, result = await worker.post(
            "/v1/files/read",
            {
                "context": worker.context | {"environment_id": "env2", "session_id": "s2"},
                "path": payload["path"],
                "offset": size - 8,
                "length": 8,
            },
        )
        assert status == 200, result
        assert (result["size"], result["sha256"]) == (size, sha)
        assert base64.b64decode(result["content_b64"]) == b"u" * 8
        assert result["eof"] is True
        # A final base64 quartet must not smuggle one byte above the decoded limit.
        assert payload["content_b64"].endswith("==")
        status, refused = await worker.post(
            "/v1/files/write",
            payload
            | {
                "path": "shared/uploads/s1/oversized/source.pdf",
                "content_b64": payload["content_b64"][:-2] + "A=",
            },
        )
        assert status == 400, refused
        # Immutable sandbox delivery refuses replacement even for a valid large request.
        status, result = await worker.post("/v1/files/write", payload | {"content_b64": "eA=="})
        assert status == 409, result


async def test_old_daemon_refuses_large_frame_without_breaking_existing_connection(
    tmp_path: Path,
) -> None:
    async with await Harness.start(tmp_path, daemon=True) as worker:
        payload = {"context": worker.context, "path": "small.txt", "content_b64": "eA=="}
        status, result = await worker.post("/v1/files/write", payload)
        assert status == 200, result
        # Public handshake data simulates a surviving pre-upgrade daemon.
        worker.view.channel().hello.pop("max_request_bytes")
        status, result = await worker.post(
            "/v1/files/write",
            payload
            | {
                "path": "large.txt",
                "content_b64": "eHh4" * (17 * 1024 * 1024),
            },
        )
        assert status == 422, result
        assert "recreate this environment" in result["error"]["message"]
        status, result = await worker.post(
            "/v1/files/read",
            {
                "context": worker.context,
                "path": "small.txt",
                "length": 1,
            },
        )
        assert status == 200, result
        assert base64.b64decode(result["content_b64"]) == b"x"
