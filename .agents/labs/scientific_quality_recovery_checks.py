# SPDX-License-Identifier: MIT
# ruff: noqa: S101, PT027
"""Offline calibration recovery checks: no service, credentials or model calls.

Standard-library assertions keep this standalone selftest independent of pytest.
"""

from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path
from unittest import TestCase
from urllib.error import HTTPError

import recovery_acceptance as recovery

from sandbox_science.orchestrator.projects import ProjectStore


def recovery_selftest(lab_type) -> dict:
    checks = TestCase()
    with tempfile.TemporaryDirectory(prefix="ss-quality-recovery-") as raw:
        root = Path(raw)
        projects = ProjectStore(root / "instance/control")
        policy = "sha256:" + "a" * 64
        projects.create(recovery.PROJECT, "minimal", policy)
        projects.close()
        context = {
            "project_id": recovery.PROJECT,
            "session_id": "calibration-a",
            "environment_id": "env-calibration",
            "actor": "user",
        }
        environment = {
            "environment_id": context["environment_id"],
            "project_id": recovery.PROJECT,
            "profile": "minimal",
            "policy": policy,
            "image": "synthetic-image",
            "sandbox": "synthetic-sandbox",
            "state": "ready",
            "created_at": "2026-10-18T00:00:00Z",
            "updated_at": "2026-10-18T00:00:00Z",
        }
        calls = []
        evidence = {}

        class EnvironmentLab(lab_type):
            def __init__(self):
                self.path = root
                self.found = None

            def context(self, session):
                assert session == "calibration-a"
                return context

            def worker(self, method, path, body=None, *, operator=False, timeout=60):
                assert operator
                calls.append((method, path, body, timeout))
                if method == "GET" and self.found is None:
                    raise HTTPError(path, 404, "missing environment", None, io.BytesIO(b"{}"))
                if method == "POST":
                    assert path == "/v1/environments"
                    assert body == {
                        key: environment[key]
                        for key in ("environment_id", "project_id", "profile", "policy")
                    }
                    assert timeout == 180
                    self.found = environment.copy()
                return self.found

            def evidence(self, name, value):
                evidence[name] = value

            def remember_sandboxes(self):
                calls.append(("owned-inventory",))

        lab = EnvironmentLab()
        lab.ensure_environment("calibration-a")
        assert [call[0] for call in calls] == ["GET", "POST", "owned-inventory"]
        calls.clear()
        lab.ensure_environment("calibration-a")
        assert [call[0] for call in calls] == ["GET", "owned-inventory"]
        lab.found = {**environment, "policy": "sha256:" + "b" * 64}
        with checks.assertRaisesRegex(AssertionError, "does not match"):
            lab.ensure_environment("calibration-a")

        original = {
            "operation_id": "reference-calibration-a",
            "context": context,
            "execution_id": "synthetic-original",
        }

        class ReferenceLab(EnvironmentLab):
            def read(self, sql, args=()):
                return []

            def ensure_environment(self, session):
                calls.append(("environment-ready",))

            def worker(self, method, path, body=None, *, operator=False, timeout=60):
                assert method == "POST"
                assert operator
                calls.append((path, body))
                if path == "/v1/operations/lookup":
                    assert body["operation_id"] == original["operation_id"]
                    assert body["context"]["environment_id"] == context["environment_id"]
                    return {"execution": self.found, "provision": None}
                assert path == "/v1/executions"
                assert calls[-2] == ("environment-ready",)
                pinned = root / "calibration-a-reference-request.json"
                assert body == json.loads(pinned.read_text())
                return original

        lab = ReferenceLab()
        calls.clear()
        assert lab.calibration_reference("calibration-a") == original
        assert [call[0] for call in calls] == [
            "/v1/operations/lookup",
            "environment-ready",
            "/v1/executions",
        ]
        marker = root / "calibration-a-seed-intent.json"
        original_marker = marker.read_bytes()
        lab.found = original
        calls.clear()
        assert lab.calibration_reference("calibration-a", resume=True) == original
        assert [call[0] for call in calls] == ["/v1/operations/lookup", "environment-ready"]
        assert marker.read_bytes() == original_marker

        # Simulate the original helper's missing-environment refusal: it wrote
        # the identity marker but no full request pin and admitted no operation.
        (root / "calibration-a-reference-request.json").unlink()
        lab.found = None
        calls.clear()
        assert lab.calibration_reference("calibration-a", resume=True) == original
        assert calls[-1][0] == "/v1/executions"
        assert marker.read_bytes() == original_marker

        (root / "calibration-a-document-intent.json").write_text("{}")
        calls.clear()
        with checks.assertRaisesRegex(AssertionError, "document/reviewer stages"):
            lab.calibration_reference("calibration-a", resume=True)
        assert not calls

        secret = "never-record-this-synthetic-token"  # noqa: S105 - synthetic redaction witness
        error = HTTPError(
            "http://synthetic.invalid",
            404,
            "not found",
            None,
            io.BytesIO(
                json.dumps(
                    {
                        "error": {
                            "kind": "not_found",
                            "message": f"missing {root}/worker; Bearer {secret}",
                        }
                    }
                ).encode()
            ),
        )
        lab.http_failure("GET", "/v1/environments/env-calibration?private=value", error)
        diagnostic = next(value for name, value in evidence.items() if "http-error" in name)
        assert diagnostic["status"] == 404
        assert diagnostic["error"]["kind"] == "not_found"
        assert diagnostic["path"] == "/v1/environments/env-calibration"
        assert secret not in json.dumps(diagnostic)
        assert str(root) not in json.dumps(diagnostic)
        return {
            "environment_create_or_get": True,
            "mismatch_refused": True,
            "original_operation_reused": True,
            "known_missing_reference_recovered": True,
            "later_stage_replay_refused": True,
            "bounded_sanitized_http_error": True,
        }
