# SPDX-License-Identifier: MIT
"""Caller authentication and attribution levels (D67)."""

import pytest

from sandbox_science.mcp_proxy.callers import Caller, Callers, attribute, token_digest

ORCHESTRATOR = Caller("orchestrator", "orchestrator", "crypto")
SHARED = Caller("crypto-default", "worker", "crypto", container="ss-crypto-default")
DEDICATED = Caller("crypto-s42", "worker", "crypto", container="ss-crypto-s42", session="s-42")
CALLERS = Callers(
    [
        (token_digest("test-secret-orchestrator"), ORCHESTRATOR),
        (token_digest("test-secret-shared"), SHARED),
    ]
)


def test_a_known_bearer_token_names_its_caller() -> None:
    assert CALLERS.authenticate("Bearer test-secret-orchestrator") is ORCHESTRATOR
    assert CALLERS.authenticate("bearer test-secret-shared") is SHARED


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Bearer test-secret-unknown",
        "Bearer test-secret-orchestrator ",
        "Basic dGVzdC1zZWNyZXQtb3JjaGVzdHJhdG9y",
        "test-secret-orchestrator",
        "Bearer " + "x" * 5000,
    ],
)
def test_anything_else_is_unauthenticated(header: str | None) -> None:
    assert CALLERS.authenticate(header) is None


def test_token_digest_is_hex_sha256() -> None:
    assert token_digest("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_attribution_levels() -> None:
    assert ORCHESTRATOR.level == "orchestrator"
    assert SHARED.level == "container"
    assert DEDICATED.level == "sandbox"


def test_the_orchestrators_session_label_is_its_statement() -> None:
    attribution = attribute(ORCHESTRATOR, ["s-1"])
    assert (attribution.session, attribution.session_claim) == ("s-1", None)
    assert attribute(ORCHESTRATOR, []).session is None


def test_a_shared_containers_label_is_only_a_claim() -> None:
    attribution = attribute(SHARED, ["s-2"])
    assert (attribution.session, attribution.session_claim) == (None, "s-2")
    assert attribution.record() == {
        "level": "container",
        "project": "crypto",
        "caller": "crypto-default",
        "kind": "worker",
        "container": "ss-crypto-default",
        "session": None,
        "session_claim": "s-2",
        "run": None,
        "run_claim": None,
    }


def test_a_dedicated_container_is_attributed_to_its_own_session() -> None:
    assert attribute(DEDICATED, []).session == "s-42"
    assert attribute(DEDICATED, ["s-42"]).session_claim is None
    attribution = attribute(DEDICATED, ["s-9"])
    assert (attribution.session, attribution.session_claim) == ("s-42", "s-9")


@pytest.mark.parametrize("labels", [["a", "b"], [""], ["s 1"], ["../x"], ["x" * 129]])
def test_malformed_labels_are_refused(labels: list[str]) -> None:
    with pytest.raises(ValueError, match="Sandbox-Science-Session"):
        attribute(SHARED, labels)
    with pytest.raises(ValueError, match="Sandbox-Science-Run"):
        attribute(ORCHESTRATOR, ["s-1"], labels)


def test_only_the_run_service_names_the_run() -> None:
    """DA-4: the run service's run is its statement; a worker's is a claim, which nothing
    vouches for, as a shared container's session is."""
    child = "20261024T101500000000Z-run-child"
    attribution = attribute(ORCHESTRATOR, ["s-1"], [child])
    assert (attribution.run, attribution.run_claim) == (child, None)
    assert attribute(ORCHESTRATOR, ["s-1"]).run is None
    for worker in (SHARED, DEDICATED):
        claimed = attribute(worker, [], [child])
        assert (claimed.run, claimed.run_claim) == (None, child)
        assert claimed.record()["run"] is None
