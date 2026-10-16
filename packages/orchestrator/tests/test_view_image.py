# SPDX-License-Identifier: MIT
"""``view_image``: a recorded PNG or JPEG shown to the run's model (docs/specs/figures.md,
FG-32 to FG-38), offered only when the model accepts images, sent in each client's form,
framed as data, and kept in the view until the run ends. The gateway is a fake LiteLLM
in this process."""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from pathlib import Path
from typing import Any, cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator import images
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.model import ModelGateway
from sandbox_science.orchestrator.notices import IMAGES
from sandbox_science.orchestrator.view_image import MAX_SIDE, MAX_VIEWS, ImageViewer, dimensions
from sandbox_science.testing.pdfs import make_pdf

from .loop_harness import API_KEY, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_artifact_inventory import SOURCE, record

Json = dict[str, Any]


def png(width: int = 4, height: int = 3) -> bytes:
    """A PNG's signature and header chunk: all ``view_image`` reads of it."""
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(
        ">IIBBBBB", width, height, 8, 2, 0, 0, 0
    ) + b"\x00" * 16  # fmt: skip


def jpeg(width: int = 5, height: int = 6) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def recorded(harness: Harness, path: str, data: bytes, operation: str = "op1") -> None:
    """A file a call of the session wrote, recorded as an artifact revision (RA-27)."""
    documents = DocumentStore(store_path(harness.state))
    try:
        source = SOURCE.model_copy(update={"context": harness.context()})
        record(ArtifactInventory(documents), operation, data, source=source, path=path)
    finally:
        documents.close()


def test_the_header_gives_the_type_and_size() -> None:
    assert dimensions(png(640, 480)) == ("image/png", 640, 480)
    assert dimensions(jpeg(800, 600)) == ("image/jpeg", 800, 600)
    assert dimensions(b"<svg/>") is None
    assert dimensions(b"\xff\xd8\xff") is None


@pytest.mark.asyncio
async def test_an_alias_accepts_images_only_when_every_deployment_does() -> None:
    data = [
        {"model_name": "a", "model_info": {"supports_vision": True}},
        {"model_name": "a", "model_info": {"supports_vision": False}},
        {"model_name": "b", "model_info": {"supports_vision": True}},
        {"model_name": "c", "model_info": {}},
    ]

    async def answer(request: web.Request) -> web.Response:
        return web.json_response({"data": data})

    app = web.Application()
    app.router.add_get("/model/info", answer)
    async with TestServer(app) as server:
        found = ModelGateway(str(server.make_url("")), API_KEY)
        try:
            assert await found.image_input() == {"a": False, "b": True, "c": False}
        finally:
            await found.close()


def _image_parts(messages: list[Json]) -> list[Json]:
    return [
        part
        for message in messages
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if part.get("type") in ("image_url", "input_image", "image")
    ]


@pytest.mark.asyncio
async def test_a_plot_is_seen_in_the_chat_client_and_framed_as_data(harness: Harness) -> None:
    harness.gateway.limits = {"main": {"supports_vision": True}}
    recorded(harness, "plots/t.png", png())
    script = [Reply(calls=[call("view_image", path="plots/t.png")]), Reply("It shows a line.")]
    first = await harness.run("v1", script)
    assert first.summaries["view_image"] == {"offered": True, "why": "gateway"}
    sent = harness.gateway.requests["main"][-1]
    assert "view_image" in {tool["function"]["name"] for tool in harness.gateway.tools["main"][0]}
    result, framed = sent[-2], sent[-1]
    assert result["role"] == "tool"
    assert "plots/t.png r1" in result["content"]
    assert framed["role"] == "user"
    text, image = framed["content"]
    assert IMAGES.text in text["text"]  # after the archive's header line (CM-4a)
    shown = json.loads(text["text"].split(IMAGES.text, 1)[1])
    assert (shown["path"], shown["revision"], shown["media_type"]) == (
        f"sessions/{SESSION}/plots/t.png", 1, "image/png",
    )  # fmt: skip
    assert image["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(png()).decode()
    (viewed,) = [cast(Json, r["viewed"]) for r in first.tools if "viewed" in r]
    assert (viewed["sha256"], viewed["width"], viewed["height"]) == (shown["sha256"], 4, 3)
    # The next run sees the image's line, not the image: its run, and its tokens, ended.
    await harness.run("v2", [Reply("Noted.")])
    later = harness.gateway.requests["main"][-1]
    assert _image_parts(later) == []
    assert any(IMAGES.text in str(m.get("content", "")) for m in later)


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["responses", "messages"])
async def test_each_client_sends_the_image_its_own_way(harness: Harness, api: str) -> None:
    harness.gateway.limits = {"main": {"supports_vision": True}}
    recorded(harness, "plots/t.jpg", jpeg())
    script = [Reply(calls=[call("view_image", path="plots/t.jpg")]), Reply("Fine.")]
    await harness.run("v1", script, options=["--api", f"main={api}"])
    data = base64.b64encode(jpeg()).decode()
    if api == "responses":
        items = harness.gateway.responses[-1]["input"]
        parts = [p for i in items if isinstance(i.get("content"), list) for p in i["content"]]
        (image,) = [p for p in parts if p.get("type") == "input_image"]
        assert image["image_url"] == f"data:image/jpeg;base64,{data}"
    else:
        (image,) = _image_parts(harness.gateway.claude[-1]["messages"])
        assert image["source"] == {"type": "base64", "media_type": "image/jpeg", "data": data}


@pytest.mark.asyncio
async def test_no_view_image_without_a_model_that_accepts_images(harness: Harness) -> None:
    harness.gateway.limits = {"main": {"supports_vision": False}}
    record = await harness.run("v1", [Reply("No images here.")])
    assert record.summaries["view_image"] == {"offered": False, "why": "gateway_says_no"}
    assert "view_image" not in {t["function"]["name"] for t in harness.gateway.tools["main"][0]}
    refused = await harness.run(
        "v2", [Reply(calls=[call("view_image", path="x.png")]), Reply("Done.")]
    )
    assert refused.tools[0]["error"] == "invalid_request"  # no such tool
    declared = await harness.run("v3", [Reply("Yes.")], options=["--images", "main"])
    assert declared.summaries["view_image"] == {"offered": True, "why": "declared"}


@pytest.mark.asyncio
async def test_what_view_image_refuses(tmp_path: Path) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    context = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")
    viewer = ImageViewer(inventory, context)
    done = record(inventory, "op1", png(), path="ok.png").revision
    assert done is not None
    record(inventory, "op2", b"<svg/>", path="plot.svg")
    record(inventory, "op3", png(2000, 10), path="wide.png")
    inventory.record_metadata(SOURCE, "big.png", sha256="c" * 64, size=9,
                              media_type="image/png", note="too_large")  # fmt: skip
    for args, kind in (
        ({"path": "plot.svg"}, "unsupported"),
        ({"path": "wide.png"}, "unsupported"),
        ({"path": "big.png"}, "unsupported"),  # drawn only by a worker, and none is here
        ({"path": "gone.png"}, "not_found"),
        ({"path": "ok.png", "revision": 1}, "invalid_request"),
    ):
        assert (await viewer.call(args)).record == {"error": kind}, args
    by_id = await viewer.call({"artifact_id": done.artifact_id, "revision": 1})
    assert by_id.notice is not None
    assert by_id.token is not None
    for _ in range(MAX_VIEWS - 1):
        assert (await viewer.call({"path": "ok.png"})).token is not None
    assert (await viewer.call({"path": "ok.png"})).record == {"error": "unavailable"}
    viewer.close()
    assert images.carried([{"role": "user", images.IMAGE: by_id.token}]) == {}
    documents.close()


def test_a_call_carries_the_newest_eight_images() -> None:
    tokens = [images.keep(images.Image("image/png", png())) for _ in range(images.IN_VIEW + 1)]
    messages = [{"role": "user", "content": "line", images.IMAGE: token} for token in tokens]
    carried = images.carried(messages)
    assert set(carried) == {id(message) for message in messages[1:]}
    sent = images.chat(messages)
    assert sent[0] == {"role": "user", "content": "line"}  # the oldest goes as its line
    assert all(isinstance(message["content"], list) for message in sent[1:])
    images.forget(tokens)
    assert images.carried(messages) == {}


@pytest.mark.asyncio
async def test_a_pdf_figure_is_drawn_in_the_environment(harness: Harness) -> None:
    """FG-35: a PDF the session wrote is drawn by the worker's helper, from the file in
    the environment, as a PNG within the limits; the drawing is in no workspace. Once the
    file changes there, its earlier revision is no longer drawn."""
    harness.gateway.limits = {"main": {"supports_vision": True}}
    pdf = base64.b64encode(make_pdf([[(72, 700, "Timing")]])).decode()
    write = f"mkdir -p plots && echo {pdf} | base64 -d > plots/fig.pdf"
    script = [
        Reply(calls=[call("shell", script=write)]),
        Reply(calls=[call("view_image", path="plots/fig.pdf")]),
        Reply("A page of text."),
    ]
    record = await harness.run("d1", script)
    sent = harness.gateway.requests["main"][-1]
    text, image = sent[-1]["content"]
    assert IMAGES.text in text["text"]
    drawn = base64.b64decode(image["image_url"]["url"].removeprefix("data:image/png;base64,"))
    width, height = struct.unpack(">II", drawn[16:24])
    assert max(width, height) <= MAX_SIDE
    (viewed,) = [cast(Json, r["viewed"]) for r in record.tools if "viewed" in r]
    assert viewed["media_type"] == "image/png"
    assert cast(Json, viewed["drawn"])["sha256"] == hashlib.sha256(drawn).hexdigest()
    first = viewed["artifact_id"]
    other = base64.b64encode(make_pdf([[(72, 700, "Changed")]])).decode()
    again = [
        Reply(calls=[call("shell", script=f"echo {other} | base64 -d > plots/fig.pdf")]),
        Reply(calls=[call("view_image", artifact_id=first, revision=1)]),
        Reply("Changed."),
    ]
    later = await harness.run("d2", again)
    refused = [r for r in later.tools if r.get("tool") == "view_image"]
    assert refused[0]["error"] == "unavailable"
