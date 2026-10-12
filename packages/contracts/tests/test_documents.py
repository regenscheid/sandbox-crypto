# SPDX-License-Identifier: MIT
"""Canonical, bounded control-owned document publication messages."""

import base64
import hashlib
import json
from typing import cast

import pytest
from pydantic import ValidationError

from sandbox_science.contracts.documents import (
    MAX_CONTENT_CHUNK,
    CatalogChange,
    CatalogContentChunk,
    CatalogDelta,
    CatalogSnapshot,
    CatalogSnapshotPage,
    PublicationEntry,
    canonical_entry,
    generation_id,
    manifest_sha256,
)


def entry(**changes: object) -> PublicationEntry:
    return PublicationEntry.model_validate(
        {
            "document_id": "doc-a",
            "revision": 1,
            "scope": "project",
            "name": "proof",
            "content_sha256": hashlib.sha256(b"hi").hexdigest(),
            "content_bytes": 2,
            **changes,
        }
    )


def target(**changes: object) -> CatalogSnapshot:
    digest = manifest_sha256("p1", 1, [entry()])
    return CatalogSnapshot.model_validate(
        {
            "project_id": "p1",
            "catalog_seq": 1,
            "entry_count": 1,
            "total_content_bytes": 2,
            "manifest_sha256": digest,
            "generation_id": generation_id(1, digest),
            **changes,
        }
    )


@pytest.mark.parametrize("value", [True, False, 1.0, "1"])
@pytest.mark.parametrize("field", ["catalog_seq", "entry_count", "total_content_bytes", "protocol"])
def test_snapshot_numbers_are_exact_integers(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        target(**{field: value})


@pytest.mark.parametrize("value", [True, 1.0, "1", 0, -1])
def test_revision_is_a_positive_exact_integer(value: object) -> None:
    with pytest.raises(ValidationError):
        entry(revision=value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "../escape"),
        ("name", "a/b"),
        ("name", ".hidden"),
        ("document_id", "../doc"),
        ("content_sha256", "A" * 64),
        ("content_sha256", "0" * 63),
        ("content_bytes", 1048577),
        ("content_bytes", True),
        ("deleted", 1),
        ("path", "other.md"),
        ("session_id", "s1"),
        ("scope", "unknown"),
    ],
)
def test_metadata_rejects_ambiguous_and_unbounded_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        entry(**{field: value})


def test_paths_are_derived_and_scope_bound() -> None:
    assert entry().path == "documents/proof.md"
    assert entry(scope="session", session_id="s1").path == "documents/sessions/s1/proof.md"
    with pytest.raises(ValidationError):
        entry(scope="session")
    assert entry(deleted=True, content_sha256=None, content_bytes=0).deleted
    with pytest.raises(ValidationError):
        entry(deleted=True)


def test_canonical_manifest_binds_project_cursor_counts_and_metadata() -> None:
    document = entry()
    header = (
        b'{"catalog_seq":1,"entry_count":1,"project_id":"p1","protocol":1,'
        b'"total_content_bytes":2}\n'
    )
    expected_entry = (
        json.dumps(document.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    assert canonical_entry(document) == expected_entry
    digest = hashlib.sha256(header + expected_entry).hexdigest()
    assert manifest_sha256("p1", 1, [document]) == digest
    assert manifest_sha256("p2", 1, [document]) != digest
    assert manifest_sha256("p1", 2, [document]) != digest
    assert target().generation_id == generation_id(1, digest)
    with pytest.raises(ValidationError):
        target(generation_id="reused")
    for documents in [
        [document, document],
        [entry(document_id="z"), document],
        [document, entry(document_id="z")],
        [entry(deleted=True, content_sha256=None, content_bytes=0)],
    ]:
        with pytest.raises(ValueError, match="ordered unique live"):
            manifest_sha256("p1", 1, documents)
    with pytest.raises(ValidationError):
        manifest_sha256("p1", True, [])
    with pytest.raises(ValidationError):
        generation_id(cast(int, cast(object, 1.0)), digest)
    with pytest.raises(ValidationError):
        generation_id(1, "A" * 64)


def test_pages_and_delta_sequences_are_bounded() -> None:
    one = entry()
    snapshot = target()
    page = CatalogSnapshotPage(
        project_id="p1", generation_id=snapshot.generation_id, start_index=0, entries=(one,)
    )
    assert page.entries == (one,)
    with pytest.raises(ValidationError):
        CatalogSnapshotPage.model_validate({**page.model_dump(), "start_index": True})
    with pytest.raises(ValidationError):
        CatalogSnapshotPage.model_validate({**page.model_dump(), "entries": [one] * 65})
    change = CatalogChange(catalog_seq=1, entry=one)
    delta = CatalogDelta(target=snapshot, after_seq=0, changes=(change,))
    assert delta.changes == (change,)
    with pytest.raises(ValidationError):
        CatalogDelta(target=snapshot, after_seq=1, changes=(change,))


@pytest.mark.parametrize("encoded", ["", "aGk=\n", "aGk", "aGm=", "_w==", "é", "@@@@"])
def test_noncanonical_base64_is_refused(encoded: str) -> None:
    with pytest.raises(ValidationError):
        CatalogContentChunk(
            project_id="p1",
            generation_id="g",
            content_sha256="0" * 64,
            offset=0,
            content_b64=encoded,
        )


def test_content_chunk_bounds_are_exact() -> None:
    fields = {"project_id": "p1", "generation_id": "g", "content_sha256": "0" * 64, "offset": 0}
    encoded = base64.b64encode(b"x" * MAX_CONTENT_CHUNK).decode()
    assert CatalogContentChunk.model_validate({**fields, "content_b64": encoded}).offset == 0
    for value in [True, 0.0, "0", -1, 1048577]:
        with pytest.raises(ValidationError):
            CatalogContentChunk.model_validate({**fields, "offset": value, "content_b64": "eA=="})
    with pytest.raises(ValidationError):
        CatalogContentChunk.model_validate(
            {**fields, "content_b64": base64.b64encode(b"x" * (MAX_CONTENT_CHUNK + 1)).decode()}
        )
