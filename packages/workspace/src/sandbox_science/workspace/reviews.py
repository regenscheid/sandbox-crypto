# SPDX-License-Identifier: MIT
"""Authenticated scoped review previews and explicit user finding decisions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast

from aiohttp import hdrs, web

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import USER, DocumentError
from sandbox_science.orchestrator.documents.mathjobs import MathJobs

from . import api, documents

# Persisted heterogeneous review JSON is bounded by the review store.
Json = dict[str, Any]


def add_routes(app: web.Application) -> None:
    base = "/api/v0/projects/{project}/sessions/{session}"
    app.router.add_get(base + "/math-checks", math_checks)
    app.router.add_get(base + "/math-checks/{check}", math_evidence)
    app.router.add_get(base + "/reviews", listing)
    app.router.add_post(base + "/reviews", request_review)
    app.router.add_get(base + "/reviews/{review}", manifest)
    app.router.add_get(base + "/reviews/{review}/records", records)
    app.router.add_get(base + "/reviews/{review}/evidence/{evidence}", evidence)
    app.router.add_get(base + "/findings", findings)
    app.router.add_get(base + "/findings/{finding}", finding)
    app.router.add_post(base + "/findings/{finding}", respond)
    app.router.add_get(base + "/findings/{finding}/responses/{response}", response_body)


def integer(request: web.Request, name: str, default: int, maximum: int) -> int:
    value = request.query.get(name, str(default))
    if not value.isascii() or not value.isdigit() or len(value) > 10:
        raise DocumentError("invalid_request", f"invalid {name}")
    parsed = int(value)
    if parsed > maximum:
        raise DocumentError("invalid_request", f"invalid {name}")
    return parsed


async def answer(
    request: web.Request, call: Callable[[ReviewStore, str, str], Json], *, writer: bool = False
) -> web.Response:
    project, session = request.match_info["project"], request.match_info["session"]
    document_store = request.app[documents.WRITER if writer else documents.DOCUMENTS]

    def run() -> str:
        store = ReviewStore(document_store)
        store.scope(project, session)
        return api.dumps(call(store, project, session))

    try:
        result = await asyncio.to_thread(run)
    except DocumentError as error:
        return documents.api_error(error)
    return web.Response(
        text=result, content_type="application/json", headers={hdrs.CACHE_CONTROL: "no-store"}
    )


async def listing(request: web.Request) -> web.Response:
    return await answer(
        request, lambda s, p, sid: s.listing(p, sid, after=request.query.get("after"))
    )


async def request_review(request: web.Request) -> web.Response:
    try:
        raw = await request.json()
        if raw != {}:
            raise ValueError
    except (ValueError, UnicodeDecodeError, RecursionError):
        return documents.api_error(
            DocumentError("invalid_request", "review request must be an empty object")
        )

    def queue(store: ReviewStore, project: str, session: str) -> Json:
        store.request(project, session)
        return {
            "queued": True,
            "detail": (
                "The next live main-agent safe point can service this "
                "request. Idle sessions do not start a model turn."
            ),
        }

    return await answer(request, queue, writer=True)


async def manifest(request: web.Request) -> web.Response:
    return await answer(
        request,
        lambda s, p, sid: s.manifest(
            p, sid, request.match_info["review"], after=integer(request, "after", 0, 1000000)
        ),
    )


async def records(request: web.Request) -> web.Response:
    return await answer(
        request,
        lambda s, p, sid: s.records(
            p, sid, request.match_info["review"], after=integer(request, "after", 0, 999999999)
        ),
    )


async def evidence(request: web.Request) -> web.Response:
    return await answer(
        request,
        lambda s, p, sid: s.read_evidence(
            p,
            sid,
            request.match_info["review"],
            request.match_info["evidence"],
            start=integer(request, "start", 0, 100000000),
            count=integer(request, "count", 16000, 16000),
            block=request.query.get("block"),
        ),
    )


async def findings(request: web.Request) -> web.Response:
    def listing(store: ReviewStore, project: str, session: str) -> Json:
        result = Findings(store).listing(project, session, after=request.query.get("after"))
        result["findings"] = [contexts(store, project, f) for f in result["findings"]]
        return result

    return await answer(request, listing)


async def finding(request: web.Request) -> web.Response:
    return await answer(
        request,
        lambda s, p, sid: contexts(
            s,
            p,
            Findings(s).get(
                p,
                sid,
                request.match_info["finding"],
                before=integer(request, "before", 999999999, 999999999)
                if "before" in request.query
                else None,
            ),
        ),
    )


def contexts(store: ReviewStore, project: str, finding: Json) -> Json:
    from sandbox_science.orchestrator.documents.project_macros import (
        finding_anchors,
        response_view,
        view,
    )

    with store.documents._read() as db:  # pyright: ignore[reportPrivateUsage]
        anchors = (
            finding_anchors(db, project, finding["finding_id"])
            if db is not None
            else [{"unknown": True}]
        )
    if finding["origin"] == "math":
        job = MathJobs(store).job(finding["math_check_id"])
        if job["project"] != project:
            raise DocumentError("internal", "finding check is outside its project")
        pinned = job.get("effective_macro_context")
        context = (
            {**pinned, "macros": pinned["effective"]}
            if pinned
            else view(store.documents, project, historical=True)
        )
    else:
        context = view(store.documents, project, anchors=anchors)
    finding["macro_context"] = context
    for entry in finding.get("history", []):
        entry["macro_context"] = view(store.documents, project, historical=True)
        if body := entry.get("response_body"):
            body["macro_context"] = response_view(store.documents, project, body["source"])
    return finding


async def respond(request: web.Request) -> web.Response:
    try:
        raw = await request.json()
        if not isinstance(raw, dict):
            raise ValueError
        raw = cast(Json, raw)
        if raw.get("finding") != request.match_info["finding"]:
            raise ValueError
    except (ValueError, UnicodeDecodeError, RecursionError):
        return documents.api_error(DocumentError("invalid_request", "invalid finding response"))
    return await answer(
        request, lambda s, p, sid: Findings(s).respond(p, sid, raw, USER), writer=True
    )


async def math_checks(request: web.Request) -> web.Response:
    return await answer(
        request,
        lambda s, p, sid: MathJobs(s).listing(p, after=request.query.get("after"), session=sid),
    )


async def response_body(request: web.Request) -> web.Response:
    from sandbox_science.orchestrator.consistency.response_bodies import ResponseBodies

    raw = request.match_info["response"]
    if not raw.isascii() or not raw.isdigit() or not 1 <= len(raw) <= 9 or int(raw) < 1:
        return documents.api_error(
            DocumentError("invalid_request", "invalid original response revision")
        )
    try:
        revision = (
            integer(request, "body_revision", 0, 999999999)
            if "body_revision" in request.query
            else None
        )
    except DocumentError as error:
        return documents.api_error(error)
    return await answer(
        request,
        lambda s, p, sid: ResponseBodies(s).get(
            p, sid, request.match_info["finding"], int(raw), body_revision=revision
        ),
    )


async def math_evidence(request: web.Request) -> web.Response:
    return await answer(
        request,
        lambda s, p, sid: MathJobs(s).evidence(
            p,
            request.match_info["check"],
            after=integer(request, "after", 0, 20000),
            span=request.query.get("span"),
            session=sid,
        ),
    )
