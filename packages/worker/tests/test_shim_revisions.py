# SPDX-License-Identifier: MIT
"""The shims' source revisions, pinned.

A shim's revision is the SHA-256 of its C source, comments included (``Shim.revision``).
Every research image and the trusted worker ship the libraries under that revision
(images/Containerfile, stage ``scheduler-shim``), and the worker uses an image's library
only when its revision matches the source the worker carries. A change to either file,
even to a comment, is therefore a deliberate, image-rebuilding change. It requires:
rebuilding and publishing every research image and the trusted worker image, rolling the
cluster's environments onto the new research images, and only then deploying the worker,
since a worker whose sandbox images all lack the new revision cannot start them. Update
the pins here in the same change. The sources' comments name the OpenShell release they
were written against (0.1.5); docs/WORKER.md names the one in use.
"""

from __future__ import annotations

import hashlib

import pytest

from sandbox_science.worker.backends import SCHED_SHIM, SIGNAL_SHIM, Shim

PINNED = {
    "scheduler": "8cec23b9ef03b4e0f2357ebb01223a799e1de513cad7ca3a21213d7e8a327a96",
    "signal": "357e8a4f0e091fcdf8457f7ab8382247fa0370185dada5089d3e58d9ee3976b8",
}


@pytest.mark.parametrize("shim", [SCHED_SHIM, SIGNAL_SHIM], ids=lambda shim: shim.label)
def test_shim_sources_keep_the_revision_the_published_images_carry(shim: Shim) -> None:
    assert hashlib.sha256(shim.source.read_bytes()).hexdigest() == PINNED[shim.label]
    assert shim.revision == PINNED[shim.label]
