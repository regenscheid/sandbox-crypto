# SPDX-License-Identifier: MIT
"""The PDF viewer's origin and its capabilities (docs/specs/pdf-viewer.md, PV-F1 to PV-F3).

PDFs are published into the artifact inventory through its public methods, as the harness
publishes them; the preview origin is the second application, served beside the workspace.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.contracts import ArtifactRef, ExecutionContext
from sandbox_science.contracts.artifact_inventory import ArtifactPublicationSource
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.workspace import cli
from sandbox_science.workspace.app import CONTENT_SECURITY_POLICY, create_app
from sandbox_science.workspace.preview import (
    CAPABILITY_SECONDS,
    Capabilities,
    Preview,
    create_preview_app,
    own_host,
    preview_policy,
)

from .route_support import TOKEN, ClientFactory, error_of, log_in

PDF_BYTES = b"%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"
SOURCE = ArtifactPublicationSource(
    context=ExecutionContext(project_id="p1", session_id="a", environment_id="env1"),
    run_id="run1",
    tool_call="call1",
    worker_endpoint_sha256="a" * 64,
)


def publish(inventory: ArtifactInventory, operation: str, path: str, data: bytes, media: str):
    worker = ArtifactRef(
        artifact_id=f"art-{operation}",
        project_id="p1",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        media_type=media,
        source_path=path,
        produced_by="attempt-1",
        created_at=datetime.now(UTC),
    )
    inventory.begin(SOURCE, operation, path, None)
    inventory.pin(SOURCE, operation, path, None, worker)
    return inventory.finish(SOURCE, operation, path, None, worker, data).revision


@pytest.fixture
def published(state_dir: Path) -> dict[str, Any]:
    documents = DocumentStore(store_path(state_dir))
    inventory = ArtifactInventory(documents)
    pdf = publish(inventory, "op-pdf", "paper.pdf", PDF_BYTES, "application/pdf")
    text = publish(inventory, "op-txt", "notes.txt", b"plain notes", "text/plain")
    documents.close()
    return {"pdf": pdf, "text": text}


class Clock:
    def __init__(self) -> None:
        self.now = 1_791_990_000.0

    def __call__(self) -> float:
        return self.now


@pytest_asyncio.fixture
async def origins(aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path):
    """The workspace and the preview origin, served side by side, the workspace logged in."""
    clock = Clock()
    preview = Preview(Capabilities(clock))
    workspace = await aiohttp_client(
        create_app(
            ProjectStore(state_dir, readonly=True),
            static_dir,
            login_token=TOKEN,
            preview=preview,
        )
    )
    documents = DocumentStore(store_path(state_dir), readonly=True)
    viewer = await aiohttp_client(
        create_preview_app(
            documents,
            preview.capabilities,
            workspace_origin=lambda: preview.workspace_origin,
        )
    )
    preview.origin = str(viewer.make_url("")).rstrip("/")
    preview.workspace_origin = str(workspace.make_url("")).rstrip("/")
    await log_in(workspace)
    yield workspace, viewer, preview, clock
    documents.close()


def capability_of(url: str) -> str:
    return parse_qs(url.split("#", 1)[1])["c"][0]


async def viewer_url(workspace: TestClient[Any, Any], revision: Any) -> Any:
    return await workspace.get(
        f"/api/v0/projects/p1/artifacts/{revision.artifact_id}/revisions/{revision.revision}/viewer",
        params={"sha256": revision.sha256},
    )


async def test_the_workspace_lists_published_artifacts(origins, published) -> None:
    workspace = origins[0]
    response = await workspace.get("/api/v0/projects/p1/artifacts")
    assert response.status == 200
    listing = await response.json()
    assert listing["viewer"] is True
    assert listing["next_after"] is None
    assert {(item["path"], item["media_type"]) for item in listing["artifacts"]} == {
        ("sessions/a/paper.pdf", "application/pdf"),
        ("sessions/a/notes.txt", "text/plain"),
    }


async def test_a_capability_opens_its_pdf_and_nothing_else(origins, published) -> None:
    workspace, viewer, preview, _ = origins
    response = await viewer_url(workspace, published["pdf"])
    assert response.status == 200
    link = await response.json()
    assert link["url"].startswith(f"{preview.origin}/viewer/#")
    fragment = parse_qs(link["url"].split("#", 1)[1])
    assert fragment["w"] == [preview.workspace_origin]
    opened = await viewer.get(f"/pdf/{capability_of(link['url'])}")
    assert opened.status == 200
    assert opened.headers["Content-Type"] == "application/pdf"
    assert await opened.read() == PDF_BYTES


async def test_forged_expired_wrong_hash_and_non_pdf_capabilities_are_refused(
    origins, published
) -> None:
    workspace, viewer, preview, clock = origins
    link = await (await viewer_url(workspace, published["pdf"])).json()
    good = capability_of(link["url"])
    payload, mac = good.split(".")
    forged = f"{payload}.{('A' if mac[0] != 'A' else 'B')}{mac[1:]}"
    assert (await viewer.get(f"/pdf/{forged}")).status == 403
    other_key = Capabilities(clock).mint("p1", published["pdf"].artifact_id, 1, "0" * 64)
    assert (await viewer.get(f"/pdf/{other_key}")).status == 403  # another process's key
    wrong_hash = preview.capabilities.mint("p1", published["pdf"].artifact_id, 1, "0" * 64)
    assert (await viewer.get(f"/pdf/{wrong_hash}")).status == 404
    text = published["text"]
    not_pdf = preview.capabilities.mint("p1", text.artifact_id, text.revision, text.sha256)
    assert (await viewer.get(f"/pdf/{not_pdf}")).status == 415
    clock.now += CAPABILITY_SECONDS
    assert (await viewer.get(f"/pdf/{good}")).status == 403  # expired


async def test_the_workspace_refuses_to_mint_for_a_wrong_hash_or_a_non_pdf(
    origins, published
) -> None:
    workspace = origins[0]
    pdf = published["pdf"]
    wrong = await workspace.get(
        f"/api/v0/projects/p1/artifacts/{pdf.artifact_id}/revisions/1/viewer",
        params={"sha256": "0" * 64},
    )
    await error_of(wrong, 409, "conflict")
    await error_of(await viewer_url(workspace, published["text"]), 422, "unsupported")


async def test_the_workspace_cookie_buys_nothing_on_the_preview_origin(origins, published) -> None:
    workspace, viewer, _, _ = origins
    link = await (await viewer_url(workspace, published["pdf"])).json()
    cookies = {
        name: morsel.value
        for name, morsel in workspace.session.cookie_jar.filter_cookies(
            workspace.make_url("/")
        ).items()
    }
    assert cookies  # the logged-in session's cookie
    for path in (
        f"/pdf/{capability_of(link['url'])}",
        "/pdf/nonsense",
        "/viewer/",
        "/api/v0/projects",
    ):
        bare = await viewer.get(path, cookies={})
        with_cookie = await viewer.get(path, cookies=cookies)
        assert bare.status == with_cookie.status, path
        assert await bare.read() == await with_cookie.read(), path
        strip = {"Date", "Server"}
        assert {k: v for k, v in bare.headers.items() if k not in strip} == {
            k: v for k, v in with_cookie.headers.items() if k not in strip
        }, path


async def test_the_preview_origin_sends_its_policy_and_takes_only_reads(origins, published) -> None:
    workspace, viewer, preview, _ = origins
    link = await (await viewer_url(workspace, published["pdf"])).json()
    opened = await viewer.get(f"/pdf/{capability_of(link['url'])}")
    for response in (
        opened,
        await viewer.get("/pdf/nonsense"),
        await viewer.get("/viewer/../secrets"),
        await viewer.post("/pdf/anything"),
    ):
        policy = response.headers["Content-Security-Policy"].removesuffix("; sandbox")
        assert policy == preview_policy(preview.workspace_origin)
        assert f"frame-ancestors {preview.workspace_origin}" in policy
        assert "connect-src 'self'" in policy
        assert "font-src 'self';" in policy  # PV-2: fonts from the preview origin only
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "no-store"
    assert (await viewer.post("/pdf/anything")).status == 405
    # The PDF bytes are fetched by PDF.js, never navigated to: opened directly, they
    # download, and a browser that renders them anyway does so in a sandbox.
    assert opened.headers["Content-Disposition"] == "attachment"
    assert opened.headers["Content-Security-Policy"].endswith("; sandbox")


async def test_the_pdf_is_read_off_the_event_loop_the_workspace_shares(
    origins, published, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, viewer, _, _ = origins
    link = await (await viewer_url(workspace, published["pdf"])).json()
    capability = capability_of(link["url"])
    loop_thread = threading.current_thread()
    threads: list[threading.Thread] = []
    original = ArtifactInventory.content

    def slow(self: ArtifactInventory, *args: Any, **kwargs: Any) -> bytes:
        threads.append(threading.current_thread())
        time.sleep(0.3)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ArtifactInventory, "content", slow)
    gaps: list[float] = []
    done = asyncio.Event()

    async def ticker() -> None:
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    ticking = asyncio.create_task(ticker())
    assert (await viewer.get(f"/pdf/{capability}")).status == 200
    done.set()
    await ticking
    assert threads
    assert all(thread is not loop_thread for thread in threads)
    assert max(gaps) < 0.15, f"the event loop stalled for {max(gaps):.3f}s"


async def test_a_capability_has_one_spelling(origins, published) -> None:
    workspace, viewer, _, _ = origins
    good = capability_of((await (await viewer_url(workspace, published["pdf"])).json())["url"])
    payload, mac = good.split(".")
    last = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_".index(mac[-1])
    unused_bits = (
        mac[:-1] + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"[last ^ 1]
    )
    for variant in (
        good.replace("-", "+").replace("_", "/"),
        f"{payload}.{mac}=",
        f"{payload}=.{mac}",
        f"{payload[:4]}!{payload[4:]}.{mac}",
        f"{payload}.{mac[:5]}~{mac[5:]}",
        unused_bits,  # the same MAC bytes, with a padding bit set
        f"{'A' * 2049}.{mac}",
    ):
        if variant != good:
            assert (await viewer.get(f"/pdf/{variant}")).status == 403, variant
    assert (await viewer.get(f"/pdf/{good}")).status == 200


async def test_the_workspace_refuses_a_revision_of_other_digits(origins, published) -> None:
    pdf = published["pdf"]
    response = await origins[0].get(
        f"/api/v0/projects/p1/artifacts/{pdf.artifact_id}/revisions/%C2%B2/viewer",
        params={"sha256": pdf.sha256},
    )
    await error_of(response, 400, "invalid_request")


async def test_the_workspace_origin_never_serves_the_viewer(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    (static_dir / "viewer").mkdir()
    (static_dir / "viewer" / "index.html").write_text("<title>viewer</title>")
    (static_dir / "viewer" / "viewer.mjs").write_text("export {}")
    workspace = await aiohttp_client(
        create_app(ProjectStore(state_dir, readonly=True), static_dir, login_token=TOKEN)
    )
    for path in ("/viewer/viewer.mjs", "/viewer/index.html", "/viewer/", "/viewer"):
        response = await workspace.get(path)
        assert response.status == 404, path
        assert "viewer" not in await response.text(), path


def test_the_preview_origins_host_check_fails_closed() -> None:
    default = inspect.signature(create_preview_app).parameters["check_host"].default
    assert default is own_host
    assert own_host(cast(Any, object())) is False


async def test_the_workspace_may_frame_the_preview_origin_and_no_other(
    origins, aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    workspace, _, preview, _ = origins
    policy = (await workspace.get("/")).headers["Content-Security-Policy"]
    assert policy == f"{CONTENT_SECURITY_POLICY}; frame-src {preview.origin}"
    assert policy.count("frame-src") == 1
    plain = await aiohttp_client(
        create_app(ProjectStore(state_dir, readonly=True), static_dir, login_token=TOKEN)
    )
    assert (await plain.get("/")).headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    await log_in(plain)
    listing = await (await plain.get("/api/v0/projects/p1/artifacts")).json()
    assert listing["viewer"] is False


async def test_the_preview_origin_serves_the_viewers_bundle_by_type(
    aiohttp_client: ClientFactory, state_dir: Path, tmp_path: Path
) -> None:
    bundle = tmp_path / "viewer"
    (bundle / "cmaps").mkdir(parents=True)
    files = {
        "index.html": "text/html; charset=utf-8",
        "THIRD-PARTY-NOTICES.txt": "text/plain; charset=utf-8",
        "cmaps/UniJIS-UCS2-H.bcmap": "application/octet-stream",
    }
    for name in [*files, "decoder.wasm", "font.ttf"]:
        (bundle / name).write_bytes(b"bytes of " + name.encode())
    (tmp_path / "secret.txt").write_text("not the bundle's")
    documents = DocumentStore(store_path(state_dir), readonly=True)
    viewer = await aiohttp_client(
        create_preview_app(
            documents,
            Capabilities(Clock()),
            workspace_origin=lambda: "http://127.0.0.1:8200",
            viewer_dir=bundle,
        )
    )
    for name, media in files.items():
        response = await viewer.get(f"/viewer/{name}")
        assert response.status == 200, name
        assert response.headers["Content-Type"] == media
        assert await response.read() == b"bytes of " + name.encode()
    assert (await (await viewer.get("/viewer/")).read()) == b"bytes of index.html"
    # Nothing the viewer does not ship is served: no WebAssembly, no TrueType font, nothing
    # outside the bundle.
    for path in ("/viewer/decoder.wasm", "/viewer/font.ttf", "/viewer/../secret.txt"):
        assert (await viewer.get(path)).status == 404, path
    documents.close()


@pytest.mark.parametrize(
    "listen",
    [
        ["--listen", "127.0.0.1:8300", "--preview-listen", "127.0.0.1:8300"],  # no own origin
        ["--preview-listen", "0.0.0.0:8301"],  # not loopback, without --allow-remote
        ["--listen", "127.0.0.1:65535"],  # no next port for the viewer's default
    ],
)
def test_the_viewer_needs_its_own_loopback_origin(
    tmp_path: Path, static_dir: Path, listen: list[str]
) -> None:
    with pytest.raises(SystemExit) as refused:
        cli.main(["serve", "--state-dir", str(tmp_path), "--static", str(static_dir), *listen])
    assert refused.value.code == 2
