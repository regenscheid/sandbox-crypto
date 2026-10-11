# SPDX-License-Identifier: MIT
"""A stand-in for an executor that research code has taken over (test_robustness.py).

It speaks the executor's stdio protocol, then misbehaves as its argument says.
The modes copy the probes of the 2026-10-11 review of the worker.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from typing import Any

MODE = sys.argv[1]
MiB = 1024 * 1024
out = sys.stdout.buffer


def raw(data: bytes) -> None:
    out.write(data)
    out.flush()


def send(message: dict[str, Any]) -> None:
    raw(json.dumps(message).encode() + b"\n")


def reply(request_id: object, **fields: Any) -> None:
    send({"type": "reply", "id": request_id, "ok": True, **fields})


def event(execution: str, kind: str, **fields: Any) -> None:
    send({"type": "event", "execution": execution, "event": kind, **fields})


def end(execution: str, **result: Any) -> None:
    now = time.time()
    fields = {"sandbox_started": now - 1, "sandbox_ended": now, "elapsed_seconds": 1.0}
    event(execution, "end", state="succeeded", result={**fields, **result})


# Lines that ended the worker's reader, each followed by an honest end (review H3, M3).
BAD_LINES = [
    b'{"type":"event","execution":"x","event":"stream","text":"\xff"}\n',
    b'{"type":"reply","id":"abc","ok":true}\n',
    b'{"type":"reply","id":true,"ok":true}\n',
    b"[" * 100000 + b"]" * 100000 + b"\n",
    b'{"type":"event","n":' + b"1" * 5000 + b"}\n",
    b'{"type":"event","pad":"' + b"a" * (17 * MiB) + b'"}\n',
    b"[" + b"{}," * 200_000 + b"{}]\n",
    b"\n",
    b"null\n",
]


def run(execution: str) -> None:
    event(execution, "state", state="running")
    if MODE == "inf":
        end(execution, elapsed_seconds=float("inf"))  # json.dumps writes Infinity
    elif MODE == "big_float":
        line = json.dumps(
            {"type": "event", "execution": execution, "event": "end", "state": "succeeded"}
        )
        raw(line[:-1].encode() + b',"result":{"elapsed_seconds":1e999,"exit_code":0}}\n')
    elif MODE == "surrogate":
        stdout = {"name": "stdout", "text": "\ud800 and \udfff", "total_bytes": 9}
        end(execution, stdout=stdout, written_files=["\udcff.txt"])
    elif MODE == "huge_numbers":
        end(execution, exit_code=10**300, signal=-5, execution_count=True, elapsed_seconds=-3)
    elif MODE == "bad_lines":
        for line in BAD_LINES:
            raw(line)
        end(execution, stdout={"name": "stdout", "text": "after\n", "total_bytes": 6})
    elif MODE == "flood":
        chunk = "x" * MiB
        for _ in range(int(os.environ.get("FLOOD_MIB", "64"))):
            event(execution, "stream", name="stdout", text=chunk)
        end(execution)
    elif MODE == "tinyflood":
        line = json.dumps(
            {"type": "event", "execution": execution, "event": "stream", "name": "stdout"}
        )
        tiny = line[:-1].encode() + b',"text":"x"}\n'
        raw(tiny * int(os.environ.get("FLOOD_N", "1000")))
        end(execution)
    elif MODE == "foreign":
        # Events for an execution of another environment, then its own end.
        target = os.environ["TARGET"]
        event(target, "stream", name="stdout", text="forged\n")
        end(target, stdout={"name": "stdout", "text": "forged result\n", "total_bytes": 14})
        end(execution)
    else:
        end(execution)


reads = 0


def read(request: dict[str, Any]) -> None:
    global reads
    reads += 1
    if reads > 8:  # a worker that keeps asking would otherwise fill its disk
        error = {"kind": "unavailable", "message": "no more ranges"}
        send({"type": "reply", "id": request["id"], "ok": False, "error": error})
        return
    offset = int(request.get("offset", 0))
    chunk = b"A" * (8 * MiB)
    fields: dict[str, Any] = {"modified": time.time(), "offset": offset, "eof": False}
    if MODE == "noeof":  # a consistent file, but no range ever says it is the last
        fields.update(size=len(chunk), content_b64=base64.b64encode(chunk).decode())
        fields["sha256"] = hashlib.sha256(chunk).hexdigest()
    elif MODE == "sizelie":  # claims one byte, sends megabytes with a matching hash
        whole = chunk * 4
        fields.update(size=1, content_b64=base64.b64encode(chunk).decode())
        fields["sha256"] = hashlib.sha256(whole).hexdigest()
    elif MODE == "badb64":
        fields.update(size=1, sha256="0" * 64, content_b64="a", eof=True)
    elif MODE == "badoffset":
        fields.update(size=len(chunk), content_b64=base64.b64encode(chunk[:10]).decode())
        fields.update(offset=offset + 5, sha256=hashlib.sha256(chunk).hexdigest())
    elif MODE == "shrinks":  # a file that keeps shrinking under publication
        size = 3 * len(chunk) - offset
        fields.update(size=size, content_b64=base64.b64encode(chunk).decode())
        fields["sha256"] = hashlib.sha256(chunk * 3).hexdigest()
    else:
        data = b"hello"
        fields.update(size=len(data), content_b64=base64.b64encode(data).decode(), eof=True)
        fields["sha256"] = hashlib.sha256(data).hexdigest()
    reply(request["id"], **fields)


for line in sys.stdin.buffer:
    request = json.loads(line)
    op = request.get("op")
    if op == "hello":
        location: object = {"hostname": "hostile"}
        if MODE == "hello_bad":
            location = "not-an-object"
        reply(request["id"], protocol=1, languages=["python"], location=location)
    elif op in ("shell", "cell"):
        reply(request["id"])
        run(request["execution"])
    elif op == "read":
        read(request)
    elif op == "cancel":
        reply(request["id"], found=False)  # it ignores cancellation
    else:
        error = {"kind": "invalid_request", "message": f"unknown operation {op!r}"}
        send({"type": "reply", "id": request.get("id"), "ok": False, "error": error})
