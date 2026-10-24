# SPDX-License-Identifier: MIT
"""docs/WORKER.md's route tables list exactly the routes the worker API registers."""

from __future__ import annotations

import re
from pathlib import Path

from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.documents_catalog import DocumentsCatalog
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.permission_requests import PermissionDesk
from sandbox_science.worker.permissions import Permissions
from sandbox_science.worker.settings import LocalBackendSettings
from sandbox_science.worker.store import Store

DOC = Path(__file__).resolve().parents[3] / "docs" / "WORKER.md"
# A table row whose first cell starts with a route in code: `GET /v1/jobs?...`.
_ROW = re.compile(r"^\|\s*`(GET|POST|PUT|PATCH|DELETE) (/v1/[^`?\s]*)")
_PARAMETER = re.compile(r"\{[^}]*\}")

Route = tuple[str, str]


def _canonical(method: str, path: str) -> Route:
    """Path parameters compare by position, whatever the doc or the code names them."""
    return method, _PARAMETER.sub("{}", path)


def documented_routes() -> set[Route]:
    routes: set[Route] = set()
    for line in DOC.read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line)
        if match:
            routes.add(_canonical(match[1], match[2]))
    return routes


async def registered_routes(tmp_path: Path) -> set[Route]:
    store = Store(tmp_path / "worker.sqlite3")
    catalog = DocumentsCatalog(tmp_path / "catalog.sqlite3")
    manager = ExecutionManager(
        store,
        LocalBackend(LocalBackendSettings(), tmp_path),
        Blobs(tmp_path / "artifacts"),
        pool_id="test",
        max_artifact_size=1024,
        state_dir=tmp_path,
    )
    try:
        # The document-catalog and permission routes are registered only with a catalog
        # and a permission desk: include them.
        desk = PermissionDesk(Permissions(tmp_path / "events.db"), list)
        app = create_app(
            manager, "test-secret-pool-token", document_catalog=catalog, permission_desk=desk
        )
        return {
            _canonical(route.method, route.resource.canonical)
            for route in app.router.routes()
            if route.method != "HEAD" and route.resource is not None
        }
    finally:
        await manager.close()
        catalog.close()
        store.close()


async def test_worker_md_lists_every_registered_route_and_no_other(tmp_path: Path) -> None:
    documented = documented_routes()
    registered = await registered_routes(tmp_path)
    assert registered, "the worker app registered no routes"
    doc = DOC.relative_to(DOC.parents[1])
    missing = sorted(registered - documented)
    assert not missing, f"update {doc}: the worker registers routes its tables omit: {missing}"
    stale = sorted(documented - registered)
    assert not stale, f"update {doc}: its tables list routes the worker does not register: {stale}"
