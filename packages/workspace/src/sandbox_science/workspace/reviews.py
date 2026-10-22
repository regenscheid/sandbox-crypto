# SPDX-License-Identifier: MIT
"""Authenticated scoped review previews and explicit user finding decisions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from aiohttp import web

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.response_bodies import ResponseBodies
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import USER, DocumentError
from sandbox_science.orchestrator.mathchecks.contexts import finding_macro_contexts
from sandbox_science.orchestrator.mathchecks.jobs import MathJobs

from . import errors
from .context import json_body
from .requests import document_store, json_response

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
    selected = document_store(request, access="write" if writer else "read")

    def run() -> Json:
        store = ReviewStore(selected)
        store.scope(project, session)
        return call(store, project, session)

    return await json_response(request, run)


async def listing(request: web.Request) -> web.Response:
    return await answer(
        request, lambda s, p, sid: s.listing(p, sid, after=request.query.get("after"))
    )


async def request_review(request: web.Request) -> web.Response:
    if await json_body(request, "review request") != {}:
        return errors.to_response(
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
    """The finding with the macro contexts it is shown with."""
    found = finding_macro_contexts(store, project, finding)
    finding["macro_context"] = found.finding.model_dump(mode="json", exclude_none=True)
    for entry in finding.get("history", []):
        entry["macro_context"] = found.history.model_dump(mode="json", exclude_none=True)
        if body := entry.get("response_body"):
            body["macro_context"] = found.responses[body["source"]].model_dump(
                mode="json", exclude_none=True
            )
    return finding


async def respond(request: web.Request) -> web.Response:
    raw = await json_body(request, "finding response")
    if not isinstance(raw, dict) or cast(Json, raw).get("finding") != request.match_info["finding"]:
        return errors.to_response(DocumentError("invalid_request", "invalid finding response"))
    raw = cast(Json, raw)
    return await answer(
        request, lambda s, p, sid: Findings(s).respond(p, sid, raw, USER), writer=True
    )


async def math_checks(request: web.Request) -> web.Response:
    return await answer(
        request,
        lambda s, p, sid: MathJobs(s).listing(p, after=request.query.get("after"), session=sid),
    )


async def response_body(request: web.Request) -> web.Response:
    raw = request.match_info["response"]
    if not raw.isascii() or not raw.isdigit() or not 1 <= len(raw) <= 9 or int(raw) < 1:
        return errors.to_response(
            DocumentError("invalid_request", "invalid original response revision")
        )
    try:
        revision = (
            integer(request, "body_revision", 0, 999999999)
            if "body_revision" in request.query
            else None
        )
    except DocumentError as error:
        return errors.to_response(error)
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
