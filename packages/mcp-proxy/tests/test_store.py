# SPDX-License-Identifier: MIT
"""The policy store: revisions by digest, and the revision each project is bound to."""

import hashlib
from pathlib import Path

import pytest

from sandbox_science.mcp_proxy.errors import StoreError
from sandbox_science.mcp_proxy.store import PolicyStore

POLICY = b"""\
apiVersion: sandbox-science/v0alpha1
kind: ProjectPolicy
metadata:
  name: store-test
"""


def test_a_project_is_bound_to_a_stored_revision(tmp_path: Path) -> None:
    store = PolicyStore(tmp_path)
    loaded = store.add_policy(POLICY)
    assert loaded.digest == "sha256:" + hashlib.sha256(POLICY).hexdigest()
    assert (tmp_path / "policies" / f"{loaded.digest[7:]}.yaml").read_bytes() == POLICY
    assert store.binding("crypto") is None
    store.bind("crypto", loaded.digest)
    assert store.binding("crypto") == loaded.digest
    assert store.policy(loaded.digest).policy.metadata.name == "store-test"
    assert store.unbind("crypto")
    assert not store.unbind("crypto")
    assert store.binding("crypto") is None


def test_the_layout_is_the_middlewares(tmp_path: Path) -> None:
    # The middleware's store binds sandboxes beside the same policies directory.
    store = PolicyStore(tmp_path)
    loaded = store.add_policy(POLICY)
    store.bind("crypto", loaded.digest)
    assert (tmp_path / "projects" / "crypto.json").read_text() == (
        f'{{"policy": "{loaded.digest}"}}\n'
    )


def test_only_stored_revisions_can_be_bound(tmp_path: Path) -> None:
    store = PolicyStore(tmp_path)
    with pytest.raises(StoreError, match="no policy"):
        store.bind("crypto", "sha256:" + "0" * 64)
    with pytest.raises(StoreError, match="not a policy revision"):
        store.bind("crypto", "sha256:short")


def test_a_file_that_does_not_hold_its_revision_is_refused(tmp_path: Path) -> None:
    store = PolicyStore(tmp_path)
    loaded = store.add_policy(POLICY)
    path = tmp_path / "policies" / f"{loaded.digest[7:]}.yaml"
    path.write_bytes(POLICY.replace(b"store-test", b"other-name"))
    with pytest.raises(StoreError, match="does not hold"):
        PolicyStore(tmp_path).policy(loaded.digest)


def test_invalid_policies_are_not_stored(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="invalid"):
        PolicyStore(tmp_path).add_policy(POLICY + b"unknown: 1\n")


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"[]",
        b'{"policy": 1}',
        b'{"policy": "sha256:abc"}',
        b'{"policy": "sha256:' + b"0" * 64 + b'", "extra": 1}',
        b" " * 5000,
    ],
)
def test_malformed_bindings_are_refused(tmp_path: Path, content: bytes) -> None:
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "crypto.json").write_bytes(content)
    with pytest.raises(StoreError):
        PolicyStore(tmp_path).binding("crypto")


@pytest.mark.parametrize("project", ["", "../crypto", "a/b", ".hidden", "x" * 129])
def test_project_ids_are_checked(tmp_path: Path, project: str) -> None:
    with pytest.raises(StoreError, match="project id"):
        PolicyStore(tmp_path).binding(project)
