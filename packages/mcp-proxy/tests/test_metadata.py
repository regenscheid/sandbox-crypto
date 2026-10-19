# SPDX-License-Identifier: MIT
"""Authenticated passive metadata stays bounded and never invokes an MCP tool."""

import json
from typing import Any

from sandbox_science.contracts.mcp_metadata import McpServiceMetadata
from sandbox_science.mcp_proxy.metadata import Metadata, structure

from .test_proxy import PLACEHOLDER


def test_schema_projection_preserves_large_integers_without_float_conversion() -> None:
    huge = 10**400
    assert structure(
        {
            "minimum": -huge,
            "maximum": huge,
            "minLength": False,
            "maxLength": float("inf"),
            "minItems": float("nan"),
            "maxItems": 1.5,
        }
    ) == {"minimum": -huge, "maximum": huge, "maxItems": 1.5}
    cache = Metadata()
    cache.listing(
        "p",
        "s",
        None,
        "a" * 64,
        {"tools": [{"name": "large_integer", "inputSchema": {"maximum": huge}}]},
        first=True,
    )
    assert json.loads(cache.tools[("p", "s")][2][0].parameters_json) == {"maximum": huge}


async def test_metadata_is_authenticated_scoped_passive_and_current(harness: Any) -> None:
    assert harness.client is not None
    path = "/metadata/papers"
    assert (await harness.client.get(path)).status == 401
    assert (
        await harness.client.get(
            path, headers={"Authorization": "Bearer " + harness.tokens["shared"]}
        )
    ).status == 403
    headers = {"Authorization": "Bearer " + harness.tokens["orchestrator"]}
    before = len(harness.fake.seen)
    response = await harness.client.get(path, headers=headers)
    assert response.status == 200
    unknown = McpServiceMetadata.model_validate_json(await response.text())
    assert unknown.status == "unknown"
    assert unknown.tools == ()
    assert len(harness.fake.seen) == before
    session = await harness.initialize()
    await harness.call(session, "tools/list")
    seen = len(harness.fake.seen)
    response = await harness.client.get(path, headers=headers)
    body = await response.text()
    found = McpServiceMetadata.model_validate_json(body)
    assert found.status == "listed"
    assert found.listing_at is not None
    assert found.tools
    assert found.policy.deny == ("admin_*",)
    assert found.policy.authentication == "bearer"
    assert PLACEHOLDER not in body
    assert "https://" not in body
    assert len(harness.fake.seen) == seen
    revised = harness.bind(harness.policy(default="deny"))
    current = await (await harness.client.get(path, headers=headers)).json()
    assert current["policy"]["revision"] == revised.digest
    assert current["listing_policy"] != revised.digest
    assert len(harness.fake.seen) == seen
    assert (
        await harness.client.get(path, headers={**headers, "Origin": "https://example.test"})
    ).status == 403
    assert (
        await harness.client.get(
            path, headers={"Authorization": "Bearer " + harness.tokens["other"]}
        )
    ).status == 503


def test_schema_projection_drops_addresses_defaults_and_deep_payloads() -> None:
    from datetime import UTC, datetime

    cache = Metadata()
    cache.listing(
        "p",
        "s",
        datetime.now(UTC),
        "a" * 64,
        {
            "tools": [
                {
                    "name": "lookup",
                    "description": "At https://private.test/api Bearer test-secret-hidden",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "default": "test-secret-default",
                                "examples": ["test-secret-example"],
                            }
                        },
                    },
                }
            ]
        },
        first=True,
    )
    tool = cache.tools[("p", "s")][2][0]
    encoded = tool.model_dump_json()
    assert "test-secret-" not in encoded
    assert "private.test" not in encoded
    assert json.loads(tool.parameters_json)["properties"]["query"] == {"type": "string"}
