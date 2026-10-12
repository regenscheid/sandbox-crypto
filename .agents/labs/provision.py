# SPDX-License-Identifier: MIT
"""Lab 18: provisioning through a real worker, middleware and package cache.

Run with the repository's Python and -I. State and evidence stay in the lab directory;
the script reads its synthetic client token but never prints or copies it.
"""

import argparse
import asyncio
import json
import os
import re
import shlex
import time
from pathlib import Path

import aiohttp
import yaml

from sandbox_science.worker import resolution
from sandbox_science.worker.catalog import load_catalog

URL = "http://127.0.0.1:8100"
CACHE = "http://host.openshell.internal:8090"
PROJECT = "lab18"
LAB = Path.home() / ".local/state/openshell-lab"
EVIDENCE = LAB / "evidence"
STAMP = str(time.time_ns())
ROOT = Path(__file__).resolve().parents[2]


def require(condition, details=None):
    if not condition:
        raise RuntimeError(f"lab check failed: {details!r}")


def save(label, data):
    EVIDENCE.mkdir(exist_ok=True)
    (EVIDENCE / f"{label}.json").write_text(json.dumps(data, indent=2) + "\n")


def context(env):
    return {"project_id": PROJECT, "session_id": f"{PROJECT}-{env}", "environment_id": env}


async def request(http, method, path, body=None):
    async with http.request(method, URL + path, json=body) as response:
        data = await response.json()
        if response.status not in (200, 201):
            raise RuntimeError(f"{method} {path}: {response.status}: {data}")
        return data


async def environment(http, suffix, *, policy=None, profile="minimal"):
    env = f"{PROJECT}-{suffix}"
    start = time.monotonic()
    data = await request(
        http,
        "POST",
        "/v1/environments",
        {
            "environment_id": env,
            "project_id": PROJECT,
            "profile": profile,
            "policy": policy or (LAB / "policy.digest").read_text().strip(),
        },
    )
    save(f"environment-{suffix}", data)
    print(f"{env}: {data['state']} in {time.monotonic() - start:.2f}s", flush=True)


async def shell(http, label, env, script, *, timeout=180):
    start = time.monotonic()
    execution = await request(
        http,
        "POST",
        "/v1/executions",
        {
            "operation_id": f"{label}-{STAMP}",
            "context": context(env),
            "kind": "shell",
            "script": script,
            "timeout_seconds": timeout,
        },
    )
    execution_id = execution["execution_id"]
    async with http.get(f"{URL}/v1/executions/{execution_id}/events") as response:
        response.raise_for_status()
        async for line in response.content:
            if not line.strip():
                continue
            event = json.loads(line)
            if event["type"] == "result":
                done = event["execution"]
                save(label, done)
                print(f"{label}: {done['state']} in {time.monotonic() - start:.2f}s", flush=True)
                return done
    raise RuntimeError(f"{label}: no result")


def output(execution):
    return (execution.get("result") or {}).get("stdout", {}).get("text", "")


async def begin_provision(http, label, env, **specs):
    return await request(
        http,
        "POST",
        "/v1/provisioning",
        {
            "operation_id": f"{label}-{STAMP}",
            "context": context(env),
            "purpose": f"Lab 18: {label}",
            **specs,
        },
    )


async def provision(http, label, env, **specs):
    return await follow(http, label, await begin_provision(http, label, env, **specs))


async def follow(http, label, operation, *, stop_permission=True):
    start = time.monotonic()
    previous = None
    while True:
        save(label, operation)
        state = operation["state"]
        if state != previous:
            print(f"{label}: {state} ({time.monotonic() - start:.2f}s)", flush=True)
            previous = state
        if state == "finished" or (state == "awaiting_permission" and stop_permission):
            print(
                f"{label}: {operation['outcome']}; "
                f"revision {operation['revision_before']} -> {operation['revision_after']}; "
                f"{len(operation['files'])} files; "
                f"probes {[(p['probe'], p['status']) for p in operation['probes']]}; "
                f"reason {operation['reason']}; error {operation['error']}",
                flush=True,
            )
            return operation
        if time.monotonic() - start > 900:
            raise RuntimeError(f"{label}: operation exceeded the lab's wait")
        operation = await request(
            http,
            "GET",
            f"/v1/provisioning/{operation['provision_id']}?after={operation['seq']}&wait=10",
        )


async def basics(http):
    a, c = f"{PROJECT}-a", f"{PROJECT}-c"
    denied = await provision(http, "unlisted-channel", a, conda=["bioconda::samtools"])
    require((denied["outcome"], denied["stage"]) == ("denied", "resolve"), denied)
    agpl = await provision(http, "agpl-conda", a, conda=["pymupdf"])
    require((agpl["outcome"], agpl["reason"]) == ("denied", "agpl_license"), agpl)
    dry = await shell(http, "dry-conda", a, shlex.join(resolution.conda_dry_run(["pari"])))
    require(dry["state"] == "succeeded", dry)
    resolved = resolution.parse_conda_dry_run(output(dry), CACHE)
    require(resolved and all(item.file.sha256 and item.file.license for item in resolved))
    save("parsed-conda", [item.file.model_dump(mode="json") for item in resolved])
    gp, flint = await asyncio.gather(
        provision(http, "gp-install", a, capability="gp"),
        provision(http, "flint-install", c, capability="python-flint"),
    )
    await verify_basics(http, gp, flint)


async def verify_basics(http, gp=None, flint=None):
    a, c = f"{PROJECT}-a", f"{PROJECT}-c"
    records = []
    for label, operation in (("gp-install", gp), ("flint-install", flint)):
        if operation is None:
            prior = json.loads((EVIDENCE / f"{label}.json").read_text())
            operation = await request(http, "GET", f"/v1/provisioning/{prior['provision_id']}")
            operation = await follow(http, label, operation)
        records.append(operation)
    gp, flint = records
    require(gp["outcome"] == "completed", gp)
    require(flint["outcome"] == "completed", flint)
    require((gp["revision_before"], gp["revision_after"]) == (0, 1), gp)
    require(not gp["notes"] and not flint["notes"], (gp["notes"], flint["notes"]))
    again = await provision(http, "gp-again", a, capability="gp")
    require(again["outcome"] == "already_satisfied" and again["revision_after"] == 1, again)
    absent_gp, absent_flint = await asyncio.gather(
        shell(http, "gp-absent-in-c", c, "command -v gp"),
        shell(http, "flint-absent-in-a", a, "python -c 'import flint'"),
    )
    require(absent_gp["state"] == absent_flint["state"] == "failed")
    history = await shell(http, "conda-history", a, 'tail -12 "$CONDA_PREFIX/conda-meta/history"')
    print(output(history), flush=True)
    for env in (a, c):
        revisions = await request(http, "GET", f"/v1/environments/{env}/revisions")
        save(f"revisions-{env}", revisions)
        lock = await request(http, "GET", f"/v1/environments/{env}/revisions/1/lockfile")
        save(f"lockfile-{env}-1", lock)
        require(CACHE not in json.dumps(lock))


async def pip_checks(http):
    env = f"{PROJECT}-a"
    dry = await shell(http, "dry-pip", env, shlex.join(resolution.pip_dry_run(["snowballstemmer"])))
    require(dry["state"] == "succeeded", dry)
    resolved = resolution.parse_pip_report(output(dry), CACHE)
    require(resolved and all(item.file.sha256 for item in resolved))
    save("parsed-pip", [item.file.model_dump(mode="json") for item in resolved])
    operation = await provision(
        http,
        "pip-install",
        env,
        pip=["snowballstemmer"],
        pip_reason="Exercise the wheels-only fallback path in a disposable lab environment",
    )
    require(operation["outcome"] == "completed", operation)
    agpl = await provision(
        http, "agpl-pip", env, pip=["pymupdf"], pip_reason="Exercise PyPI license checking"
    )
    require((agpl["outcome"], agpl["reason"]) == ("denied", "agpl_license"), agpl)


async def command(*argv, check=True):
    process = await asyncio.create_subprocess_exec(
        *map(str, argv), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    stdout, _ = await process.communicate()
    text = stdout.decode(errors="replace")
    if check and process.returncode:
        raise RuntimeError(f"{argv[0]} exited {process.returncode}: {text}")
    return process.returncode, text


async def operator(action, seq, *, check=True):
    code, text = await command(
        ROOT / ".venv/bin/sandbox-science-policy-middleware",
        action,
        "--settings",
        LAB / "middleware/middleware.toml",
        "--request",
        str(seq),
        check=check,
    )
    save(f"operator-{action}-{seq}", {"exit_code": code, "output": text})
    print(text, flush=True)
    return code, text


async def cache_log(label, since, sandbox):
    _, text = await command(
        "journalctl",
        "--user",
        "-u",
        "openshell-lab-cache",
        "--since",
        f"@{since}",
        "--no-pager",
        "-o",
        "cat",
    )
    lines = [line for line in text.splitlines() if f"sandbox={sandbox}" in line]
    files = [line for line in lines if re.search(r"\.(conda|tar\.bz2) result=", line)]
    save(label, {"lines": lines, "package_files": files})
    return files


def fetch_script(url, *, attempts=1):
    return (
        "python - <<'PY'\n"
        + (
            "import time, urllib.request, urllib.error\n"
            f"for _ in range({attempts}):\n"
            "    try:\n"
            f"        with urllib.request.urlopen({url!r}, timeout=5) as response:\n"
            "            print('ALLOWED', response.status, flush=True)\n"
            "    except urllib.error.HTTPError as error:\n"
            "        print('REFUSED', error.code, error.read(4096).decode(), flush=True)\n"
            "    time.sleep(0.25)\n"
        )
        + "PY\n"
    )


async def approvals(http):
    policy = yaml.safe_load((ROOT / "examples/policy.yaml").read_text())
    policy["packages"]["conda"]["action"] = "escalate"
    path = LAB / "escalate.yaml"
    path.write_text(yaml.safe_dump(policy))
    _, text = await command(
        ROOT / ".venv/bin/sandbox-science-worker",
        "policy",
        "add",
        "--settings",
        LAB / "worker/worker.toml",
        "--project",
        PROJECT,
        path,
    )
    digest = re.search(r"sha256:[0-9a-f]{64}", text).group()
    await environment(http, "e", policy=digest)
    env = f"{PROJECT}-e"
    sandbox = json.loads((EVIDENCE / "environment-e.json").read_text())["sandbox_id"]
    start = int(time.time())
    denied = await provision(http, "fpylll-deny", env, capability="fpylll")
    require(denied["state"] == "awaiting_permission", denied)
    seq = denied["permission_request"]
    await operator("pending", seq)
    await operator("deny", seq)
    denied = await follow(http, "fpylll-deny", denied, stop_permission=False)
    require(denied["outcome"] == "denied", denied)
    require(not await cache_log("denied-cache", start, sandbox))
    start = int(time.time())
    approved = await provision(http, "fpylll-approve", env, capability="fpylll")
    require(approved["state"] == "awaiting_permission", approved)
    seq = approved["permission_request"]
    await operator("pending", seq)
    gp = json.loads((EVIDENCE / "gp-install.json").read_text())
    file = next(file for file in gp["files"] if file["name"] == "pari")
    other_url = f"{CACHE}/conda/{file['channel']}/{file['subdir']}/{file['file']}"
    intruder = asyncio.create_task(
        shell(http, "outside-grant", env, fetch_script(other_url, attempts=32))
    )
    await asyncio.sleep(1)
    await operator("approve", seq)
    approved = await follow(http, "fpylll-approve", approved, stop_permission=False)
    require(approved["outcome"] == "completed", approved)
    done = await intruder
    require(done["state"] == "succeeded" and "ALLOWED" not in output(done), done)
    require("REFUSED" in output(done), done)
    files = await cache_log("approved-cache", start, sandbox)
    paths = {f"/conda/{f['channel']}/{f['subdir']}/{f['file']}" for f in approved["files"]}
    require(files and all(any(f"path={path} " in line for path in paths) for line in files))
    print(f"approved downloads: {len(files)} requests for {len(paths)} decided files", flush=True)
    file = approved["files"][0]
    url = f"{CACHE}/conda/{file['channel']}/{file['subdir']}/{file['file']}"
    ended = await shell(http, "grant-ended", env, fetch_script(url))
    require("REFUSED" in output(ended) and "ALLOWED" not in output(ended), ended)
    await operator("pending", seq)
    code, _ = await operator("approve", seq, check=False)
    require(code != 0)


async def missing_build(http):
    gp = json.loads((EVIDENCE / "gp-install.json").read_text())
    file = next(file for file in gp["files"] if file["name"] == "pari")
    operation = await provision(
        http,
        "missing-build",
        f"{PROJECT}-a",
        conda=[f"pari=={file['version']}=ss_lab18_missing_build"],
    )
    require(operation["outcome"] == "failed" and operation["stage"] == "resolve", operation)


async def probes(http):
    operation = await provision(http, "flint-with-sage", f"{PROJECT}-p", capability="python-flint")
    require(operation["outcome"] in ("completed", "already_satisfied"), operation)
    catalog = load_catalog()
    for name, capability in catalog.capabilities.items():
        for probe in capability.probes:
            done = await shell(
                http,
                f"catalog-{name}",
                f"{PROJECT}-p",
                shlex.join(probe.argv),
                timeout=probe.timeout_seconds,
            )
            passed = done["state"] == "succeeded" and probe.passes(output(done))
            print(
                f"catalog-{name}: {'passed' if passed else 'FAILED'} {output(done)!r}", flush=True
            )
            require(passed, done)


async def interrupt(http, *, restart):
    """Observe real Conda records appearing, then interrupt the still-running install."""
    suffix = "restart" if restart else "cancel"
    await environment(http, suffix)
    env = f"{PROJECT}-{suffix}"
    info = json.loads((EVIDENCE / f"environment-{suffix}.json").read_text())
    cli_env = dict(os.environ)
    cli_env.update(
        XDG_CONFIG_HOME=str(LAB / "config"),
        XDG_STATE_HOME=str(LAB / "state"),
        XDG_DATA_HOME=str(LAB / "data"),
        OPENSHELL_LOCAL_TLS_DIR=str(LAB / "state/openshell/tls"),
        OPENSHELL_TELEMETRY_ENABLED="false",
    )
    monitor = (
        "import json, pathlib, time\n"
        "p = pathlib.Path('/opt/sandbox-science/conda-meta')\n"
        "before = {f.name for f in p.glob('*.json')}\n"
        "print('READY', len(before), flush=True)\n"
        "until = time.monotonic() + 600\n"
        "while time.monotonic() < until:\n"
        "    added = sorted({f.name for f in p.glob('*.json')} - before)\n"
        "    if len(added) >= 3:\n"
        "        print(json.dumps({'linked_records': added, 'time': time.time()}), flush=True)\n"
        "        break\n"
        "    time.sleep(0.002)\n"
        "else:\n"
        "    raise SystemExit('no linking observed')\n"
    )
    observer = await asyncio.create_subprocess_exec(
        Path.home() / ".local/opt/openshell-0.1.3/bin/openshell",
        "sandbox",
        "exec",
        "-n",
        info["sandbox"],
        "--no-tty",
        "--no-login-shell",
        "--",
        "/opt/sandbox-science/bin/python",
        "-I",
        "-c",
        monitor,
        env=cli_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        ready = await asyncio.wait_for(observer.stdout.readline(), 30)
        require(ready.startswith(b"READY"), ready)
        print(f"{suffix}: observer {ready.decode().strip()}", flush=True)
        operation = await begin_provision(
            http,
            f"interrupt-{suffix}",
            env,
            conda=["sympy", "networkx", "matplotlib-base", "scipy"],
        )
        save(f"interrupt-{suffix}", operation)
        changed = await asyncio.wait_for(observer.stdout.readline(), 600)
        evidence = json.loads(changed)
        save(f"link-observer-{suffix}", evidence)
        operation = await request(http, "GET", f"/v1/provisioning/{operation['provision_id']}")
        require(operation["state"] == "installing", operation)
        save(f"before-interrupt-{suffix}", operation)
        print(
            f"{suffix}: saw {len(evidence['linked_records'])} new records while installing",
            flush=True,
        )
        if restart:
            await command("systemctl", "--user", "restart", "openshell-lab-worker")
            for _ in range(50):
                try:
                    operation = await request(
                        http, "GET", f"/v1/provisioning/{operation['provision_id']}"
                    )
                    break
                except aiohttp.ClientConnectionError:
                    await asyncio.sleep(0.2)
        else:
            operation = await request(
                http, "POST", f"/v1/provisioning/{operation['provision_id']}/cancel"
            )
        operation = await follow(http, f"interrupt-{suffix}", operation, stop_permission=False)
        require(operation["outcome"] == "needs_recovery", operation)
        queue = await request(http, "GET", f"/v1/environments/{env}/provisioning")
        save(f"queue-{suffix}", queue)
        require(queue["status"] == "needs_recovery", queue)
        print(f"{suffix}: queue status {queue['status']}; revision {queue['revision']}", flush=True)
    finally:
        if observer.returncode is None:
            observer.terminate()
        await observer.communicate()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "parts",
        nargs="+",
        choices=(
            "envs",
            "basics",
            "verify",
            "pip",
            "approvals",
            "pins",
            "probes",
            "cancel",
            "restart",
        ),
    )
    parts = parser.parse_args().parts
    token = (LAB / "worker/client-a.token").read_text().strip()
    async with aiohttp.ClientSession(
        headers={"Authorization": f"Bearer {token}"},
        timeout=aiohttp.ClientTimeout(total=1200),
    ) as http:
        for part in parts:
            if part == "envs":
                await environment(http, "a")
                await environment(http, "c")
            elif part == "basics":
                await basics(http)
            elif part == "verify":
                await verify_basics(http)
            elif part == "pip":
                await pip_checks(http)
            elif part == "approvals":
                await approvals(http)
            elif part == "pins":
                await missing_build(http)
            elif part == "probes":
                await environment(http, "p", profile="crypto-base")
                await probes(http)
            elif part in ("cancel", "restart"):
                await interrupt(http, restart=part == "restart")


if __name__ == "__main__":
    asyncio.run(main())
