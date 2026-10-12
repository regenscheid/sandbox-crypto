"""The package cache's per-sandbox budgets, through the policy middleware's header.

Usage: budget.py TOKEN_FILE PHASE   (header: one request each, A forging a name;
                                     budget: A spends with C's name forged while C fetches)
"""

import asyncio
import json
import sys
import time
from pathlib import Path

import aiohttp

TOKEN = Path(sys.argv[1]).read_text().strip()
PHASE = sys.argv[2]
URL = "http://127.0.0.1:8100"
H = {"Authorization": f"Bearer {TOKEN}"}
STAMP = str(int(time.time()))
PROJECT = "lab17"
A, C = "lab17-a", "lab17-c"

FETCH = r"""cat > fetch.py <<'EOF'
import concurrent.futures, sys, time, urllib.error, urllib.request
names, forged = sys.argv[1].split(","), (sys.argv[2] if len(sys.argv) > 2 else "")
def fetch(name):
    started = time.monotonic()
    headers = {"X-Sandbox-Science-Sandbox": forged} if forged else {}
    request = urllib.request.Request(f"http://host.openshell.internal:8090/pypi/simple/{name}/", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except OSError as exc:
        status = type(exc).__name__
    return name, status, time.monotonic() - started
with concurrent.futures.ThreadPoolExecutor(16) as pool:
    print(", ".join(f"{name} {status} {took:.1f} s" for name, status, took in pool.map(fetch, names)))
EOF
"""


def ctx(env, session):
    return {"project_id": PROJECT, "session_id": session, "environment_id": env}


async def run(http, op, env, session, script):
    body = {
        "operation_id": f"{op}-{STAMP}",
        "context": ctx(env, session),
        "kind": "shell",
        "script": script,
    }
    started = time.monotonic()
    async with http.post(f"{URL}/v1/executions", headers=H, json=body) as r:
        execution = await r.json()
    async with http.get(f"{URL}/v1/executions/{execution['execution_id']}/events", headers=H) as r:
        async for line in r.content:
            if line.strip() and (event := json.loads(line))["type"] == "result":
                done = event["execution"]
                result = done.get("result") or {}
                text = ((result.get("stdout") or {}).get("text") or "") + (
                    (result.get("stderr") or {}).get("text") or ""
                )
                return done["state"], text.strip(), time.monotonic() - started
    raise RuntimeError(f"no result for {execution['execution_id']}")


async def sandbox_id(http, env):
    async with http.get(f"{URL}/v1/environments/{env}", headers=H) as r:
        return (await r.json())["sandbox_id"]


async def main():
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as http:
        ids = {env: await sandbox_id(http, env) for env in (A, C)}
        print("sandbox IDs:", ids)
        if PHASE == "header":
            print(
                "A, forging 'forged-by-a':",
                await run(http, "h1", A, "a", FETCH + "python3 fetch.py six forged-by-a"),
            )
            print(
                "C, no header of its own:",
                await run(http, "h2", C, "c", FETCH + "python3 fetch.py six"),
            )
        else:
            spender = ",".join(
                [
                    "requests",
                    "flask",
                    "django",
                    "click",
                    "jinja2",
                    "werkzeug",
                    "itsdangerous",
                    "blinker",
                ]
            )
            results = await asyncio.gather(
                run(http, "b1", A, "a", f"python3 fetch.py {spender} {ids[C]}"),
                run(http, "b2", C, "c", "sleep 1; python3 fetch.py httpx,anyio,sniffio"),
            )
            print("A, 8 new projects at once, forging C's sandbox ID:", results[0])
            print("C, 3 new projects, 1 s later:", results[1])


asyncio.run(main())
