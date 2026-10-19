# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Owned real-OpenShell automatic recovery probes; synthetic models, genuine Sage."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path

from sandbox_science.launcher import instance, toml
from sandbox_science.launcher.config import load_config
from sandbox_science.launcher.state import Layout

spec = importlib.util.spec_from_file_location(
    "recovery_acceptance", Path(__file__).with_name("recovery_acceptance.py")
)
assert spec is not None
assert spec.loader is not None
recovery = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = recovery
spec.loader.exec_module(recovery)


class Lab(recovery.Lab):
    def mark(self, name, detail):
        path = self.path / (name + "-intent.json")
        assert not path.exists(), "probe already admitted; reconcile its original outcome"
        recovery.private(path, json.dumps({"at": time.time(), **detail}) + "\n")

    def parts(self):
        record = instance.read(Layout(self.path / "instance"))
        assert record is not None
        assert instance.launcher_alive(record)
        return record

    def same_job(self, before):
        after = self.sage()
        for key in ("execution_id", "attempt_id", "operation_id", "started_at"):
            assert before["execution"][key] == after["execution"][key], key
        assert after["execution"]["state"] in ("running", "succeeded")
        return after

    def worker_cycle(self):
        before = self.sage()
        assert before["execution"]["state"] == "running"
        record = self.parts()
        worker = next(p for p in record.parts if p.name == "worker")
        assert instance.alive(worker, record)
        self.mark("automatic-worker", {"pid": worker.pid, "started": worker.started})
        os.kill(worker.pid, signal.SIGKILL)

        def restarted():
            now = self.parts()
            found = next(p for p in now.parts if p.name == "worker")
            return found.pid != worker.pid and instance.alive(found, now)

        recovery.wait(restarted)
        recovery.wait(self.ready)

        def ready_job():
            try:
                return self.same_job(before)
            except OSError:
                return None

        after = recovery.wait(ready_job)
        self.evidence(
            "automatic-worker",
            {"before": before, "after": after, "automatic": True, "at": time.time()},
        )
        print("PASS automatically restarted worker retained original Sage operation", flush=True)

    def tree(self, *, host=False):
        session = "tree-host" if host else "tree"
        before = self.sage()
        assert before["execution"]["state"] == "running"
        self.login()
        self.mark(session, {"message_id": "lab-" + session})
        self.post(
            session,
            "messages",
            {"message_id": "lab-" + session, "text": "TREE-LOSS: qualify recursive recovery."},
        )
        original = recovery.wait(lambda: self.shell_running(session), seconds=240)
        record = self.parts()
        self.remember_sandboxes()
        if host:
            # Simulate loss of all owned host processes, not a physical host reboot.
            assert instance.launcher_alive(record)
            os.kill(record.pid, signal.SIGKILL)
            recovery.wait(lambda: not instance.launcher_alive(record))
            unit = self.prefix + "-launcher.service"
            recovery.wait(
                lambda: (
                    recovery.run(["systemctl", "--user", "is-active", unit], check=False).returncode
                    != 0
                )
            )
            # A collected transient unit can be inactive before systemd has
            # unloaded it. Wait for that known state before reusing its name.
            recovery.wait(
                lambda: (
                    recovery.run(
                        ["systemctl", "--user", "show", "-p", "LoadState", "--value", unit]
                    ).stdout.strip()
                    == "not-found"
                )
            )
            self.unit(
                "launcher",
                [recovery.REPO / ".venv/bin/sandbox-science", "up", "--config", self.config],
            )
        else:
            vm = json.loads((self.path / "instance/orchestrator/sandbox.json").read_text())
            listed = {p["name"]: p["id"] for p in self.remember_sandboxes()}
            assert listed[vm["name"]] == vm["id"]
            self.cli("sandbox", "delete", vm["name"], timeout=120)
        recovery.wait(self.ready, seconds=300)
        self.remember_sandboxes()
        recovery.wait(lambda: self.settled(session), seconds=360)
        result = self.results(session)
        shells = [item for item in result["tools"] if item.get("tool") == "shell"]
        assert len(result["works"]) == 2
        assert len([item for item in result["intents"] if item[2] == "shell"]) == 1
        assert len(shells) == 1
        assert shells[0]["execution"]["result"]["stdout"]["text"] == "child-holdingchild-done"
        decisions = self.read(
            "SELECT identity,detail FROM research_observations "
            "WHERE session=? AND kind='auto_resume'",
            (session,),
        )
        assert decisions, "no automatic root Resume was recorded"
        self.evidence(
            session + "-automatic",
            {
                "original": original,
                "result": result,
                "decisions": decisions,
                "sage": self.same_job(before),
                "host_process_loss": host,
                "physical_host_reboot": False,
            },
        )
        print("PASS automatic root Resume after " + session + " loss", flush=True)

    def host_cycle(self):
        self.tree(host=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "prepare",
            "up",
            "start",
            "inspect",
            "tree",
            "worker_cycle",
            "host_cycle",
            "finish",
            "cleanup",
            "abort",
        ),
    )
    parser.add_argument("lab", nargs="?", type=Path)
    parser.add_argument("--duration", type=int, default=3600)
    parser.add_argument("--static", type=Path, default=recovery.REPO / "web/workspace/dist")
    args = parser.parse_args()
    if args.action == "prepare":
        assert 60 <= args.duration <= 7200
        path = recovery.prepare(args.duration, args.static)
        config_path = path / "sandbox-science.toml"
        config = load_config(config_path).model_dump(mode="json", exclude_none=True)
        config["orchestrator"]["recover_infrastructure"] = True
        config["projects"][0]["sessions"].append("tree-host")
        recovery.private(config_path, toml.dumps(config))
        load_config(config_path)
        print(path)
        return
    assert args.lab is not None
    getattr(Lab(args.lab), args.action)()


if __name__ == "__main__":
    main()
