# SPDX-License-Identifier: MIT
"""Support for the runtime manager's tests: a TLS certificate for ``localhost``, a fake
of control's manager listener, and settings that point at both.

``FakeControl`` follows KRB-7 as ``confinement/credentials.py`` does: the latest issued
request replays, an older one answers 410, and a new one revokes before it answers 409
while a runtime is live. A runtime is live while a ``runtime-<pid>.live`` marker of the
fake OpenShell CLI names a running process; revoking stops those processes, as control's
refusal at the next frame ends a real runtime.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import secrets
import signal
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

MANAGER_TOKEN = "test-secret-manager-capability-0123456789abcdef"
REVISION = "a" * 40
IMAGE = "registry.example.test/orchestrator@sha256:" + "0" * 64
KATA: dict[str, Any] = {
    "runtime_class_name": "kata",
    "node_selector": {"kubernetes.io/arch": "amd64"},
    "requests": {"cpu_millicores": 500, "memory_mib": 1024, "ephemeral_storage_mib": 1024},
    "limits": {"cpu_millicores": 2000, "memory_mib": 4096, "ephemeral_storage_mib": 4096},
}


@dataclass(frozen=True)
class LocalTls:
    """A self-signed CA certificate for ``localhost``, its key, and a server context."""

    certificate: Path
    key: Path

    def server(self) -> ssl.SSLContext:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(self.certificate, self.key)
        return context


def localhost_tls(directory: Path) -> LocalTls:
    """A throwaway certificate valid for ``localhost``, which is also its own CA."""
    directory.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = directory / "control.pem", directory / "control-key.pem"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)
    return LocalTls(cert_file, key_file)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ")[-1][0] != "Z"
    except OSError:
        return True


def live_runtimes(directory: Path) -> list[int]:
    """The PIDs of the fake CLI's runtimes still running."""
    found = []
    for marker in directory.glob("runtime-*.live"):
        pid = int(marker.name.split("-")[1].split(".")[0])
        if alive(pid):
            found.append(pid)
    return found


@dataclass
class Issue:
    request: str
    state: str
    generation: str | None = None
    token: str | None = None


@dataclass
class FakeControl:
    """Control's manager listener, with its issue journal in memory."""

    markers: Path
    """Where the fake CLI's runtime markers are."""
    instance: str = field(default_factory=lambda: secrets.token_hex(16))
    issues: list[Issue] = field(default_factory=list[Issue])
    asked: list[str] = field(default_factory=list[str])
    health_calls: int = 0
    occupied: bool | None = None
    """Forces occupancy; None reads the runtime markers."""

    def _live(self) -> bool:
        return self.occupied if self.occupied is not None else bool(live_runtimes(self.markers))

    def _revoke(self) -> bool:
        live = self._live()
        if self.occupied is None:
            for pid in live_runtimes(self.markers):
                os.kill(pid, signal.SIGTERM)
        return live

    def restart(self) -> None:
        """As a control restart: a new instance, the runtime revoked, the latest superseded."""
        self.instance = secrets.token_hex(16)
        self._revoke()
        if self.issues and self.issues[-1].state == "issued":
            self.issues[-1].state = "superseded"

    def latest(self) -> Issue:
        return self.issues[-1]

    async def health(self, request: web.Request) -> web.Response:
        self.health_calls += 1
        live = self._live()
        return web.json_response(
            {
                "ready": True,
                "process_alive": True,
                "protocol": 1,
                "runtime_connected": live,
                "runtime_ready": live,
                "runtime_draining": False,
                "control_instance": self.instance,
            }
        )

    async def issue(self, request: web.Request) -> web.Response:
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if (
            hashlib.sha256(supplied.encode()).digest()
            != hashlib.sha256(MANAGER_TOKEN.encode()).digest()
        ):
            raise web.HTTPNotFound()
        asked = str(json.loads(await request.read())["request"])
        self.asked.append(asked)
        latest = self.issues[-1] if self.issues else None
        if latest is not None and latest.request == asked and latest.state == "issued":
            return web.json_response({"generation": latest.generation, "token": latest.token})
        retry = latest is not None and latest.request == asked and latest.state == "revoked"
        if not retry:
            if any(issue.request == asked for issue in self.issues):
                return web.json_response({"error": "superseded", "message": "old"}, status=410)
            if latest is not None:
                latest.state = "superseded"
            self.issues.append(Issue(asked, "revoked"))
        if self._revoke():
            return web.json_response({"error": "occupied", "message": "busy"}, status=409)
        issue = self.issues[-1]
        issue.state = "issued"
        issue.generation = "runtime-" + secrets.token_hex(12)
        issue.token = secrets.token_hex(32)
        return web.json_response({"generation": issue.generation, "token": issue.token})

    @asynccontextmanager
    async def serving(self, tls: LocalTls) -> AsyncIterator[str]:
        """Serve on a free port; yields the manager URL."""
        app = web.Application()
        app.router.add_get("/health", self.health)
        app.router.add_post("/runtime/credentials", self.issue)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls.server())
        await site.start()
        try:
            yield f"https://localhost:{runner.addresses[0][1]}"
        finally:
            await runner.cleanup()


def private(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    return path


def settings_document(
    tmp_path: Path, *, manager_url: str, ca: Path, cli: Path | str = "openshell"
) -> dict[str, Any]:
    """Valid manager settings, as ``runtime-manager.json`` holds them."""
    models = {"main": "main", "reviewer": "review", "image_revision": REVISION, "apis": {}}
    port = int(manager_url.rsplit(":", 1)[1])
    return {
        "state_dir": str(tmp_path / "manager-state"),
        "control_url": f"https://localhost:{port + 1 if port < 65535 else port - 1}",
        "manager_url": manager_url,
        "control_ca_file": str(ca),
        "manager_token_file": str(private(tmp_path / "secret" / "manager.token", MANAGER_TOKEN)),
        "control_models_file": str(private(tmp_path / "control.json", json.dumps(models))),
        "model_url": "https://models.example.test/v1",
        "model_key_file": str(private(tmp_path / "secret" / "model.key", "test-secret-model")),
        "image": IMAGE,
        "revision": REVISION,
        "policy": {
            "worker_api": {"host": "worker.example.test", "port": 8100},
            "state_service": {"host": "control.example.test", "port": 8443},
            "model_gateway": {"host": "models.example.test", "port": 443},
            "binaries": ["/opt/sandbox-science/bin/python*"],
        },
        "runtime": KATA,
        "openshell": {"cli": str(cli)},
        "start_timeout": 20,
        "stop_timeout": 3,
    }
