# SPDX-License-Identifier: MIT
"""The worker names its protocol version on every response and refuses another one."""

from __future__ import annotations

from pathlib import Path

from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION

from .harness import AUTH, Harness


async def test_every_response_names_the_protocol_version(tmp_path: Path) -> None:
    async with await Harness.start(tmp_path) as worker:
        execution = await worker.submit("op-1", kind="shell", script="echo hi")
        url = worker.url
        for method, path, headers in (
            ("GET", "/v1/capabilities?environment_id=env1", AUTH),
            ("GET", f"/v1/executions/{execution['execution_id']}/events", AUTH),
            ("GET", "/v1/executions/exe-missing", AUTH),
            ("GET", "/v1/capabilities?environment_id=env1", {}),
        ):
            async with worker.http.request(method, url + path, headers=headers) as response:
                await response.read()
                assert response.headers[PROTOCOL_HEADER] == str(PROTOCOL_VERSION), path


async def test_a_client_of_another_version_is_refused_before_anything_runs(
    tmp_path: Path,
) -> None:
    async with await Harness.start(tmp_path) as worker:
        body = {
            "kind": "shell",
            "operation_id": "op-1",
            "context": worker.context,
            "script": "true",
        }
        for spoken in (str(PROTOCOL_VERSION + 1), "1", ""):
            async with worker.http.post(
                worker.url + "/v1/executions",
                json=body,
                headers={**AUTH, PROTOCOL_HEADER: spoken},
            ) as response:
                refused = await response.json()
            assert response.status == 422
            assert refused["error"]["kind"] == "unsupported"
            assert "version mismatch" in refused["error"]["message"]
        # Nothing was recorded: the operation ID is still free for another request. The
        # same version, or none at all (a hand-written request), is served.
        assert (await worker.submit("op-1", kind="shell", script="echo other"))[
            "operation_id"
        ] == "op-1"
        async with worker.http.post(
            worker.url + "/v1/executions", json={**body, "operation_id": "op-2"}, headers=AUTH
        ) as response:
            assert response.status == 201
