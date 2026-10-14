# SPDX-License-Identifier: MIT
"""Every write route reads its body the same way: JSON within its byte limit."""

from __future__ import annotations

from pathlib import Path

import pytest

from .route_support import ClientFactory, error_of, origin, workspace

WRITES = {
    "document change": "/api/v0/projects/p1/documents/doc/changes",
    "annotation change": "/api/v0/projects/p1/documents/doc/comments",
    "project macro JSON": "/api/v0/projects/p1/macros",
    "review request": "/api/v0/projects/p1/sessions/a/reviews",
    "finding response": "/api/v0/projects/p1/sessions/a/findings/f1",
    "plan edit": "/api/v0/projects/p1/sessions/a/plan/changes",
}


@pytest.mark.parametrize(("what", "path"), WRITES.items(), ids=list(WRITES))
async def test_a_body_that_is_not_json_is_refused(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, what: str, path: str
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await client.post(
        path,
        data=b"{not json",
        headers={**origin(client), "Content-Type": "application/json"},
    )
    error = await error_of(response, 400, "invalid_request")
    assert what in error["message"]


@pytest.mark.parametrize(
    ("what", "path", "limit"),
    [
        ("annotation change", WRITES["annotation change"], 64 * 1024),
        ("document change", WRITES["document change"], 8 * 1024 * 1024),
        ("plan edit", WRITES["plan edit"], 128 * 1024),
    ],
    ids=["comment", "document", "plan"],
)
async def test_a_body_over_its_limit_is_a_json_413(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    what: str,
    path: str,
    limit: int,
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await client.post(
        path,
        data=b" " * (limit + 1),
        headers={**origin(client), "Content-Type": "application/json"},
    )
    error = await error_of(response, 413, "invalid_request")
    assert error["message"] == f"{what} exceeds its byte limit"


@pytest.mark.parametrize(
    "raw",
    [b"\xef\xbb\xbf{}", "{}".encode("utf-16-le"), b"{}"],
    ids=["utf8-bom", "utf16", "unknown-charset"],
)
async def test_a_body_that_is_not_plain_utf8_json_is_refused(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, raw: bytes
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    charset = "; charset=no-such-charset" if raw == b"{}" else ""
    response = await client.post(
        WRITES["review request"],
        data=raw,
        headers={**origin(client), "Content-Type": "application/json" + charset},
    )
    await error_of(response, 400, "invalid_request")
