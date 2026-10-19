# SPDX-License-Identifier: MIT
"""Synthetic streamed Responses peer for the isolated recovery acceptance lab."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

from aiohttp import web


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lab", type=Path)
    args = parser.parse_args()
    lab = args.lab.resolve()
    identity = json.loads((lab / "identity.json").read_text())
    if identity["purpose"] != "disposable synthetic recovery acceptance":
        raise ValueError("not an owned recovery lab")
    key = (lab / "model.key").read_text().strip()

    async def models(_: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": name} for name in ("main", "delegated")]})

    async def info(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "data": [
                    {
                        "model_name": name,
                        "model_info": {
                            "supports_function_calling": True,
                            "max_input_tokens": 200000,
                        },
                    }
                    for name in ("main", "delegated")
                ]
            }
        )

    async def respond(request: web.Request) -> web.StreamResponse:
        if request.headers.get("Authorization") != "Bearer " + key:
            return web.Response(status=401)
        body = await request.json()
        items = body["input"]
        text = json.dumps(items)
        user_text = json.dumps([item for item in items if item.get("role") == "user"])
        calls = {item.get("call_id") for item in items if item.get("type") == "function_call"}
        stamp = str(time.time_ns())
        output = []

        fault = re.search(r"AUTO-RECOVERY(?:-[A-Z0-9]+)?", user_text)
        fault_file = lab / (
            "stream-failure-injected"
            + ("-" + fault.group() if fault and fault.group() != "AUTO-RECOVERY" else "")
        )
        if fault and not fault_file.exists():
            # End one actual streamed HTTP response before its terminal event.
            # This becomes a saved model-phase failure, rather than an HTTP
            # retry. The next call must be admitted by durable auto recovery.
            fault_file.write_text(stamp + "\n")
            with (lab / "model-observations.jsonl").open("a") as out:
                out.write(
                    json.dumps(
                        {
                            "at": time.time(),
                            "model": body["model"],
                            "stream": body.get("stream"),
                            "output": [],
                            "injected_failure": "stream ended before terminal response",
                        }
                    )
                    + "\n"
                )
            stream = web.StreamResponse(
                headers={"Content-Type": "text/event-stream", "x-litellm-response-cost": "0"}
            )
            await stream.prepare(request)
            await stream.write(b": synthetic premature EOF\n\n")
            await stream.write_eof()
            return stream

        def said(value: str) -> None:
            output.append(
                {
                    "id": "msg_" + stamp,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": value, "annotations": []}],
                }
            )

        def tool(name: str, arguments: dict, call_id: str) -> None:
            output.append(
                {
                    "id": "fc_" + stamp,
                    "type": "function_call",
                    "status": "completed",
                    "call_id": call_id,
                    "name": name,
                    "arguments": json.dumps(arguments),
                }
            )

        narrative_kind = (
            "fold"
            if "the platform is folding the oldest part" in user_text
            else "overview"
            if "the platform is merging the oldest fold" in user_text
            else None
        )
        if narrative_kind:
            said(json.dumps({"narrative": "Child-only evidence remains available.", "keys": []}))
        elif "NARRATIVE-CHILD" in user_text:
            counter_file = lab / "narrative-progress.json"
            counter = json.loads(counter_file.read_text()) if counter_file.exists() else 0
            if counter < 10:
                said("Child-only evidence. " + "observation " * 5000)
                tool("plan_read", {}, "narrative-note-" + str(counter))
                counter_file.write_text(json.dumps(counter + 1))
                # Give the real concurrent narrator time to settle before the
                # next turn. This is synthetic transport pacing, not model speed.
                await asyncio.sleep(1)
            else:
                said(
                    json.dumps(
                        {
                            "status": "complete",
                            "summary": "Child-only evidence retained.",
                            "deviations": [],
                            "open_questions": [],
                        }
                    )
                )
        elif "NARRATIVE-PARENT-PRIVATE" in user_text:
            if "narrative-delegate" not in calls:
                tool(
                    "delegate",
                    {"task": "NARRATIVE-CHILD: retain the synthetic observations and finish."},
                    "narrative-delegate",
                )
            elif "narrative-wait" not in calls:
                found = sorted(set(re.findall(r"dlg-[0-9a-f]{24}", text)))
                tool("wait_delegations", {"delegation_ids": found, "mode": "all"}, "narrative-wait")
            else:
                said("The child completed its narrative folding probe.")
        elif "AUTO-RECOVERY" in text:
            said("Automatic model-phase recovery completed.")
        elif "HISTORY-LEAF-ONLY" in user_text:
            if "leaf-proof" not in calls:
                tool(
                    "shell",
                    {"script": "printf descendant-archive-proof-981c", "timeout_seconds": 30},
                    "leaf-proof",
                )
            else:
                said(
                    json.dumps(
                        {
                            "status": "complete",
                            "summary": "Leaf task complete.",
                            "deviations": [],
                            "open_questions": [],
                        }
                    )
                )
        elif "HISTORY-TREE" in text:
            delegated = body["model"] == "delegated"
            if "history-delegate" not in calls:
                task = (
                    "HISTORY-LEAF-ONLY: produce retained evidence."
                    if delegated
                    else "HISTORY-TREE: delegate a leaf, then retrieve its full archived tool result."
                )
                tool(
                    "delegate",
                    {"task": task, **({"delegate": True} if not delegated else {})},
                    "history-delegate",
                )
            elif "history-wait" not in calls:
                found = sorted(set(re.findall(r"dlg-[0-9a-f]{24}", text)))
                tool("wait_delegations", {"delegation_ids": found, "mode": "all"}, "history-wait")
            elif delegated and "history-search" not in calls:
                tool(
                    "history_search",
                    {
                        "query": "descendant-archive-proof-981c",
                        "scope": "project",
                        "kind": "tool_result",
                    },
                    "history-search",
                )
            elif delegated and "history-read" not in calls:
                searched = next(
                    item["output"]
                    for item in reversed(items)
                    if item.get("type") == "function_call_output"
                    and item.get("call_id") == "history-search"
                )
                selected = re.findall(r'"message_id"\s*:\s*"(msg-[0-9a-f]{16})"', searched)
                if not selected:
                    raise ValueError("synthetic parent could not find descendant archive hit")
                tool("history_read", {"message": selected[0]}, "history-read")
            elif delegated:
                said(
                    json.dumps(
                        {
                            "status": "complete",
                            "summary": "Read descendant-archive-proof-981c through the archive tools.",
                            "deviations": [],
                            "open_questions": [],
                        }
                    )
                )
            else:
                said("The delegated parent retrieved the leaf's archived output.")
        elif "SAGE-LONG" in text:
            script = (lab / "sage-job.sh").read_text()
            job_call = "sage-job-" + hashlib.sha256(script.encode()).hexdigest()[:12]
            if job_call not in calls:
                tool(
                    "start_job",
                    {
                        "script": script,
                        "purpose": "Synthetic one-hour Sage computation with durable checkpoints",
                        "expected_progress": "Verified finite-field batches every 30 seconds",
                        "timeout_seconds": identity["duration"] + 600,
                        "wake_session": False,
                    },
                    job_call,
                )
            else:
                said("The sustained Sage computation was submitted; inspect its retained job ID.")
        elif "LITERAL-LOSS" in text:
            if "literal-seed" not in calls:
                tool("python", {"code": "x = 41", "replayable": True}, "literal-seed")
            elif "literal-hold" not in calls:
                tool(
                    "shell",
                    {
                        "script": "printf literal-holding; sleep 40; printf literal-done",
                        "timeout_seconds": 120,
                    },
                    "literal-hold",
                )
            elif "literal-check" not in calls:
                tool("python", {"code": "print(x + 1)"}, "literal-check")
            else:
                said("Literal reconstruction evidence is in the Python result.")
        elif "TREE-LOSS" in text:
            if body["model"] == "delegated":
                if "child-shell" not in calls:
                    tool(
                        "shell",
                        {
                            "script": "printf child-holding; sleep 40; printf child-done",
                            "timeout_seconds": 120,
                        },
                        "child-shell",
                    )
                else:
                    said(
                        json.dumps(
                            {
                                "status": "complete",
                                "summary": "Original child shell retained.",
                                "deviations": [],
                                "open_questions": [],
                            }
                        )
                    )
            elif "tree-delegate" not in calls:
                tool(
                    "delegate",
                    {"task": "TREE-LOSS: run the checkpoint shell once and report."},
                    "tree-delegate",
                )
            elif "tree-wait" not in calls:
                found = sorted(set(re.findall(r"dlg-[0-9a-f]{24}", text)))
                tool("wait_delegations", {"delegation_ids": found, "mode": "all"}, "tree-wait")
            else:
                said("The child returned its original checkpoint evidence.")
        else:
            said("Synthetic recovery fixture complete.")

        with (lab / "model-observations.jsonl").open("a") as out:
            out.write(
                json.dumps(
                    {
                        "at": time.time(),
                        "model": body["model"],
                        "stream": body.get("stream"),
                        "reasoning": body.get("reasoning"),
                        "narrative_kind": narrative_kind,
                        "contains_parent_private": "NARRATIVE-PARENT-PRIVATE" in text,
                        "input_chars": len(text),
                        "offered_calls": sorted(calls),
                        "output": output,
                    }
                )
                + "\n"
            )
        response = {
            "id": "resp_" + stamp,
            "object": "response",
            "status": "completed",
            "model": "synthetic-recovery",
            "output": output,
            "usage": {
                "input_tokens": max(10, len(text) // 4) if "NARRATIVE-" in text else 10,
                "output_tokens": max(5, len(json.dumps(output)) // 4)
                if "NARRATIVE-" in text
                else 5,
                "input_tokens_details": {"cached_tokens": 0},
            },
        }
        stream = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "x-litellm-response-cost": "0"}
        )
        await stream.prepare(request)
        await stream.write(b": synthetic keepalive\n\n")
        await stream.write(
            (
                "event: response.completed\ndata: "
                + json.dumps({"type": "response.completed", "response": response})
                + "\n\n"
            ).encode()
        )
        await stream.write_eof()
        return stream

    app = web.Application(client_max_size=16 * 1024 * 1024)
    app.router.add_get("/v1/models", models)
    app.router.add_get("/v1/model/info", info)
    app.router.add_post("/v1/responses", respond)
    web.run_app(app, host="127.0.0.1", port=identity["ports"]["model"], print=None)


if __name__ == "__main__":
    main()
