# SPDX-License-Identifier: MIT
"""A stand-in for an executor that research code has taken over (test_robustness.py,
test_provisioning.py).

It speaks the executor's stdio protocol, then misbehaves as its argument says. The
modes copy the probes of the 2026-10-11 reviews of the worker and of package commands;
``HOSTILE_COUNT`` and ``HOSTILE_ENTRIES`` size the revision modes.
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
COUNT = int(os.environ.get("HOSTILE_COUNT", "1"))
ENTRIES = int(os.environ.get("HOSTILE_ENTRIES", "100"))
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


seen: list[str] = []


def unknown_keys() -> dict[str, int]:
    """A lockfile of HOSTILE_ENTRIES keys no lockfile has (second review, M3 and L1)."""
    return {f"k{index:x}": 0 for index in range(ENTRIES)}


def run(execution: str) -> None:
    if MODE == "lockfile_requests":
        # Every fingerprint new, so that the worker keeps asking for the current lockfile;
        # revision events credited to this attempt and earlier ones of the environment
        # (second review, L1).
        seen.append(execution)
        event(execution, "state", state="running", environment=os.urandom(16).hex())
        for cause in seen[-6:]:
            event(
                execution,
                "revision",
                fingerprint=os.urandom(16).hex(),
                managed=False,
                cause=cause,
                lockfile=unknown_keys(),
            )
        time.sleep(0.3)
        end(execution, environment=os.urandom(16).hex())
        return
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
    elif MODE == "revisions":
        # Revisions by the score: the first with a lockfile naming the package cache, the
        # others credited to an attempt of no one's, with lockfiles that are none.
        cached = {"name": "x", "version": "1", "build": "0"}
        cached["channel"] = "http://host.openshell.internal:8090/conda/conda-forge"
        command = {"command": ["pip", "install", "x" * 5000], "via": "wrapper"}
        for index in range(80):
            lockfile: object = {"conda": [cached]} if index == 0 else {"conda": "none"}
            event(
                execution,
                "revision",
                fingerprint=f"{index:032x}",
                managed=True,
                cause=execution if index == 0 else "exe-" + "0" * 24,
                commands=[command],
                lockfile=lockfile,
            )
        event(execution, "revision", fingerprint="not a fingerprint", managed=True)
        end(execution)
    elif MODE == "slow_lockfile":
        # Channels of 2,047 characters in ~1,000 path segments (review C1).
        channel = ("http://h/" + "a/" * 1019)[:2047]
        conda = [
            {"name": f"p{index}", "version": "1", "build": "0", "channel": channel}
            for index in range(ENTRIES)
        ]
        event(
            execution,
            "revision",
            fingerprint=os.urandom(16).hex(),
            managed=True,
            cause=execution,
            lockfile={"conda": conda},
        )
        end(execution)
    elif MODE == "lockfiles":
        # A distinct lockfile with each of HOSTILE_COUNT revisions (review H1).
        for index in range(COUNT):
            salt = os.urandom(8).hex()
            conda = [
                {"name": f"n{salt}-{entry}", "version": "1.0", "build": "0", "sha256": "a" * 64}
                for entry in range(ENTRIES)
            ]
            event(
                execution,
                "revision",
                fingerprint=os.urandom(16).hex(),
                managed=True,
                cause=execution,
                commands=[{"command": ["pip", "install", f"n{index}"], "via": "wrapper"}],
                lockfile={"conda": conda},
            )
        end(execution)
    elif MODE == "big_commands":
        # HOSTILE_COUNT revisions with 8 commands of 40 arguments of 600 characters, of
        # four bytes each in UTF-8 (review H1).
        for index in range(COUNT):
            argv = [f"{index:04d}" + "\U0001f600" * 596 for _ in range(40)]
            event(
                execution,
                "revision",
                fingerprint=os.urandom(16).hex(),
                managed=True,
                cause=execution,
                commands=[{"command": argv, "via": "wrapper", "exit_code": 0}] * 8,
            )
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
        fields: dict[str, Any] = {}
        if MODE == "lockfile_requests":
            fields["environment"] = os.urandom(16).hex()
        reply(request["id"], protocol=1, languages=["python"], location=location, **fields)
    elif op == "lockfile" and MODE == "lockfile_requests":
        reply(request["id"], fingerprint=os.urandom(16).hex(), lockfile=unknown_keys())
    elif op == "usage":
        cpu = 10**400 if MODE == "huge_cpu" else 1.5
        reply(request["id"], cpu_seconds=cpu, kernels=0, running=0)
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
        if MODE == "deaf" and op == "probe":
            time.sleep(3600)  # stops reading its requests, as a stopped process would
