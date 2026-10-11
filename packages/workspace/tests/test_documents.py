# SPDX-License-Identifier: MIT
"""The API's living documents, from a control store the orchestrator's own store wrote.

The workspace opens the store read-only (living documents, LD-9, LD-11).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.orchestrator.documents import USER, Author, DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.workspace.app import create_app

pytestmark = pytest.mark.asyncio

TOKEN = "test-secret-login-token-0123456789abcdef"  # noqa: S105 - a test value
AGENT = Author(kind="agent", session_id="a", run_id="run-1", role="main", model="scripted")
FF = """---
title: Fermat
macros:
  \\F: \\mathbb{F}
---
# Fermat's little theorem

Let $p = 17$ and $a \\in \\F_p$.

> $$
> a^{p-1} = 1
> $$

::: lemma
Every $a$.
:::
"""

ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]


@pytest.fixture
def documents(state_dir: Path) -> dict[str, str]:
    """ff in p1 at revision 2, a workbook document of session a, and one in p2."""
    store = DocumentStore(store_path(state_dir), producer="test")
    ff = store.create("p1", name="ff", title="FF", text=FF, author=USER).document_id
    mapped = store.mapped("p1", ff)
    paragraph = next(
        block_id
        for block_id, block in zip(mapped.block_ids, mapped.parsed.blocks, strict=True)
        if block.text.startswith("Let")
    )
    store.patch(
        "p1",
        ff,
        base=1,
        author=AGENT,
        reason="primes",
        operation_id="o1",
        operations=[{"op": "replace", "block": paragraph, "markdown": "Let $p$ be prime."}],
    )
    notes = store.create(
        "p1", name="notes", title="Notes", text="Notes.\n", author=AGENT, session_id="a"
    ).document_id
    other = store.create("p2", name="ff", title="Other", text="Other.\n", author=USER).document_id
    store.close()
    return {"ff": ff, "paragraph": paragraph, "notes": notes, "other": other}


@pytest_asyncio.fixture
async def client(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> TestClient[Any, Any]:
    app = create_app(ProjectStore(state_dir), static_dir, login_token=TOKEN)
    found = await aiohttp_client(app)
    response = await found.get("/login", params={"token": TOKEN}, allow_redirects=False)
    assert response.status == 303
    return found


async def get(client: TestClient[Any, Any], path: str, status: int = 200) -> Any:
    response = await client.get(path)
    assert response.status == status, await response.text()
    assert response.headers["Cache-Control"] == "no-store"
    return await response.json()


async def test_a_projects_documents(
    client: TestClient[Any, Any], documents: dict[str, str]
) -> None:
    listed = (await get(client, "/api/v0/projects/p1/documents"))["documents"]
    assert [(found["name"], found["scope"], found["head"]) for found in listed] == [
        ("ff", "project", 2),
        ("notes", "session", 1),
    ]
    assert listed[1]["session_id"] == "a"
    assert listed[0]["created_by"]["kind"] == "user"
    other = (await get(client, "/api/v0/projects/p2/documents"))["documents"]
    assert [found["document_id"] for found in other] == [documents["other"]]


async def test_a_revision_with_its_block_map_and_formulas(
    client: TestClient[Any, Any], documents: dict[str, str]
) -> None:
    found = await get(client, f"/api/v0/projects/p1/documents/{documents['ff']}")
    assert found["document"]["title"] == "FF"
    assert (found["title"], found["macros"]) == ("Fermat", {"\\F": "\\mathbb{F}"})
    assert found["matched"] is True
    revision = found["revision"]
    assert (revision["number"], revision["reason"]) == (2, "primes")
    assert revision["author"]["kind"] == "agent"
    assert found["text"].startswith("---\ntitle: Fermat")
    blocks = {block["id"]: block for block in found["blocks"]}
    paragraph = blocks[documents["paragraph"]]
    assert paragraph["status"] == "changed"
    assert [(span["kind"], span["tex"]) for span in paragraph["math"]] == [("inline", "p")]
    kinds = [(block["kind"], block["depth"], block["lines"]) for block in found["blocks"]]
    assert kinds == [
        ("front_matter", 0, [0, 5]),
        ("heading", 0, [5, 6]),
        ("paragraph", 0, [7, 8]),
        ("quote", 0, [9, 12]),
        ("equation", 1, [9, 12]),
        ("container", 0, [13, 16]),
        ("paragraph", 1, [14, 15]),
    ]
    # A $$ block in a quote: its TeX as the parser reads it, the quote's markers kept,
    # as the browser's parser reads it too.
    equation = found["blocks"][4]
    assert equation["math"][0]["tex"] == "\n> a^{p-1} = 1\n> "
    assert found["blocks"][6]["parent"] == found["blocks"][5]["id"]
    first = await get(client, "/api/v0/projects/p1/documents/ff?revision=1")
    assert first["revision"]["number"] == 1
    assert {block["status"] for block in first["blocks"]} == {"added"}


async def test_who_changed_what(client: TestClient[Any, Any], documents: dict[str, str]) -> None:
    found = await get(client, f"/api/v0/projects/p1/documents/{documents['ff']}/history")
    assert [revision["number"] for revision in found["revisions"]] == [1, 2]
    assert found["revisions"][1]["changed"] == [documents["paragraph"]]
    assert found["revisions"][1]["author"]["run_id"] == "run-1"
    changes = found["blocks"][documents["paragraph"]]
    assert [(change["revision"], change["status"]) for change in changes] == [
        (1, "added"),
        (2, "changed"),
    ]
    assert [patch["state"] for patch in found["patches"]] == ["applied"]


async def test_a_workbook_document_by_its_id(
    client: TestClient[Any, Any], documents: dict[str, str]
) -> None:
    found = await get(client, f"/api/v0/projects/p1/documents/{documents['notes']}")
    assert found["document"]["scope"] == "session"
    assert found["text"] == "Notes.\n"


@pytest.mark.parametrize(
    ("path", "status", "kind"),
    [
        ("/api/v0/projects/missing/documents", 404, "not_found"),
        ("/api/v0/projects/p1/documents/missing", 404, "not_found"),
        ("/api/v0/projects/p1/documents/ff?revision=9", 404, "not_found"),
        ("/api/v0/projects/p1/documents/ff?revision=0", 400, "invalid_request"),
        ("/api/v0/projects/p1/documents/ff?revision=x", 400, "invalid_request"),
        ("/api/v0/projects/p1/documents/..%2Fff", 400, "invalid_request"),
        ("/api/v0/projects/p1/documents/a%20b", 400, "invalid_request"),
        ("/api/v0/projects/p1/documents/missing/history", 404, "not_found"),
    ],
)
async def test_errors_name_their_kind(
    client: TestClient[Any, Any],
    documents: dict[str, str],
    path: str,
    status: int,
    kind: str,
) -> None:
    found = await get(client, path, status)
    assert found["error"]["kind"] == kind


async def test_another_projects_document_is_not_found(
    client: TestClient[Any, Any], documents: dict[str, str]
) -> None:
    for path in (
        f"/api/v0/projects/p1/documents/{documents['other']}",
        f"/api/v0/projects/p2/documents/{documents['ff']}/history",
        f"/api/v0/projects/p2/documents/{documents['notes']}",
    ):
        assert (await get(client, path, 404))["error"]["kind"] == "not_found"


async def test_documents_need_the_session(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, documents: dict[str, str]
) -> None:
    app = create_app(ProjectStore(state_dir), static_dir, login_token=TOKEN)
    anonymous = await aiohttp_client(app)
    for path in (
        "/api/v0/projects/p1/documents",
        f"/api/v0/projects/p1/documents/{documents['ff']}",
    ):
        response = await anonymous.get(path)
        assert response.status == 401


async def test_the_workspace_never_writes_the_store(
    client: TestClient[Any, Any], state_dir: Path, documents: dict[str, str]
) -> None:
    path = store_path(state_dir)
    before = path.read_bytes()
    await get(client, "/api/v0/projects/p1/documents")
    await get(client, f"/api/v0/projects/p1/documents/{documents['ff']}")
    await get(client, f"/api/v0/projects/p1/documents/{documents['ff']}/history")
    assert path.read_bytes() == before
    # A writer goes on writing beside the reader.
    writer = DocumentStore(path, producer="test")
    writer.save_text(
        "p1", documents["notes"], base=1, text="Notes, again.\n", author=USER, reason="u"
    )
    writer.close()
    found = await get(client, f"/api/v0/projects/p1/documents/{documents['notes']}")
    assert found["text"] == "Notes, again.\n"


async def test_no_documents_yet(client: TestClient[Any, Any], state_dir: Path) -> None:
    assert not store_path(state_dir).exists()
    assert (await get(client, "/api/v0/projects/p1/documents"))["documents"] == []
    await get(client, "/api/v0/projects/p1/documents/ff", 404)
    assert not store_path(state_dir).exists()


async def test_a_revision_the_parser_no_longer_reads_alike(
    client: TestClient[Any, Any], state_dir: Path, documents: dict[str, str]
) -> None:
    # A block map another parser made: the blocks come without their formulas' TeX.
    import sqlite3

    database = sqlite3.connect(store_path(state_dir))
    database.execute("DROP TRIGGER revisions_no_update")
    stored = json.loads(
        database.execute(
            "SELECT block_map FROM revisions WHERE document_id = ? AND number = 2",
            (documents["ff"],),
        ).fetchone()[0]
    )
    stored["profile"] = "ss-markdown-0"
    database.execute(
        "UPDATE revisions SET block_map = ? WHERE document_id = ? AND number = 2",
        (json.dumps(stored), documents["ff"]),
    )
    database.commit()
    database.close()
    found = await get(client, f"/api/v0/projects/p1/documents/{documents['ff']}")
    assert found["matched"] is False
    assert found["macros"] == {}
    assert all(span["tex"] is None for block in found["blocks"] for span in block["math"])
    assert len(found["blocks"]) == 7


async def test_the_answers_are_read_in_a_thread(
    client: TestClient[Any, Any], documents: dict[str, str]
) -> None:
    paths = [f"/api/v0/projects/p1/documents/{documents['ff']}"] * 4
    answers = await asyncio.gather(*(get(client, path) for path in paths))
    assert len({json.dumps(answer, sort_keys=True) for answer in answers}) == 1
