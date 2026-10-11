# SPDX-License-Identifier: MIT
"""The policy store and sandbox bindings."""

import json
from pathlib import Path

import pytest

from sandbox_science.policy_middleware.errors import StoreError
from sandbox_science.policy_middleware.store import PolicyStore

REPO_ROOT = Path(__file__).resolve().parents[3]
POLICY = (REPO_ROOT / "examples" / "policy.yaml").read_bytes()
OTHER_DIGEST = "sha256:" + "1" * 64


def test_policies_are_stored_under_their_revision(tmp_path: Path) -> None:
    store = PolicyStore(tmp_path)
    loaded = store.add_policy(POLICY)
    path = tmp_path / "policies" / f"{loaded.digest.removeprefix('sha256:')}.yaml"
    assert path.read_bytes() == POLICY
    assert PolicyStore(tmp_path).policy(loaded.digest).digest == loaded.digest


def test_an_invalid_policy_is_not_stored(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="invalid"):
        PolicyStore(tmp_path).add_policy(b"kind: ProjectPolicy\n")
    assert not (tmp_path / "policies").exists()


def test_a_file_that_does_not_hold_its_revision_is_rejected(tmp_path: Path) -> None:
    store = PolicyStore(tmp_path)
    loaded = store.add_policy(POLICY)
    path = tmp_path / "policies" / f"{loaded.digest.removeprefix('sha256:')}.yaml"
    path.write_bytes(POLICY + b"# edited\n")
    with pytest.raises(StoreError, match="does not hold"):
        PolicyStore(tmp_path).policy(loaded.digest)


def test_an_unreadable_policy_is_reported(tmp_path: Path) -> None:
    store = PolicyStore(tmp_path)
    path = tmp_path / "policies" / f"{OTHER_DIGEST.removeprefix('sha256:')}.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("not: [a policy")
    with pytest.raises(StoreError, match="cannot be loaded"):
        store.policy(OTHER_DIGEST)
    with pytest.raises(StoreError, match="no policy"):
        store.policy("sha256:" + "2" * 64)


@pytest.mark.parametrize(
    "digest", ["", "sha256:", "sha256:" + "A" * 64, "sha1:" + "0" * 40, "sha256:../" + "0" * 61]
)
def test_malformed_revisions_are_rejected(tmp_path: Path, digest: str) -> None:
    with pytest.raises(StoreError, match="not a policy revision"):
        PolicyStore(tmp_path).policy(digest)


def test_bind_and_unbind(tmp_path: Path) -> None:
    store = PolicyStore(tmp_path)
    loaded = store.add_policy(POLICY)
    assert store.binding("sbx-1") is None
    store.bind("sbx-1", loaded.digest)
    assert store.binding("sbx-1") == loaded.digest
    assert json.loads((tmp_path / "sandboxes" / "sbx-1.json").read_text()) == {
        "policy": loaded.digest
    }
    assert store.unbind("sbx-1")
    assert not store.unbind("sbx-1")
    assert store.binding("sbx-1") is None


def test_a_sandbox_can_only_be_bound_to_a_stored_policy(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="no policy"):
        PolicyStore(tmp_path).bind("sbx-1", OTHER_DIGEST)


@pytest.mark.parametrize(
    "sandbox_id", ["", "../sbx", "a/b", ".hidden", "-flag", "x" * 129, "sbx 1", "sbx\n1"]
)
def test_malformed_sandbox_ids_are_rejected(tmp_path: Path, sandbox_id: str) -> None:
    store = PolicyStore(tmp_path)
    with pytest.raises(StoreError, match="not a valid sandbox id"):
        store.binding(sandbox_id)
    with pytest.raises(StoreError, match="not a valid sandbox id"):
        store.unbind(sandbox_id)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(b"\xff\xfe", id="not-utf8"),
        pytest.param(b'["sha256:x"]', id="not-an-object"),
        pytest.param(b"{}", id="empty"),
        pytest.param(json.dumps({"policy": OTHER_DIGEST, "x": 1}).encode(), id="extra-key"),
        pytest.param(json.dumps({"policy": 1}).encode(), id="not-a-string"),
        pytest.param(json.dumps({"policy": "sha256:abc"}).encode(), id="bad-digest"),
        pytest.param(b" " * 5000, id="too-large"),
    ],
)
def test_malformed_bindings_are_rejected(tmp_path: Path, content: bytes) -> None:
    (tmp_path / "sandboxes").mkdir()
    (tmp_path / "sandboxes" / "sbx-1.json").write_bytes(content)
    with pytest.raises(StoreError):
        PolicyStore(tmp_path).binding("sbx-1")


def test_writes_leave_no_temporary_files(tmp_path: Path) -> None:
    store = PolicyStore(tmp_path)
    loaded = store.add_policy(POLICY)
    store.bind("sbx-1", loaded.digest)
    leftovers = [path.name for path in tmp_path.rglob(".tmp-*")]
    assert leftovers == []
