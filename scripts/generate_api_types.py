# SPDX-License-Identifier: MIT
"""Generate the browser's TypeScript types for the workspace API's typed answers.

The workspace's Pydantic schemas are the API contract. This script turns model
response serialization schemas and dictionary/request validation schemas into
``web/workspace/src/lib/api-types.ts``. Run
``uv run python scripts/generate_api_types.py`` after changing a contract.
With ``--check``, as the workspace tests run it, the script changes nothing and fails
when the committed file differs from what it would generate.

Only the schema features the models use are translated: objects, arrays, tuples,
enums, constants, unions, nullable values and references. Model response fields are
required because answers carry defaults. Dictionary response and validation-mode
request schemas retain their explicit required lists and wire aliases. Constraints such
as lengths and patterns stay on the server.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter
from pydantic.json_schema import models_json_schema

from sandbox_science.contracts.classifications import (
    ClassificationAccepted,
    DriftAccepted,
)
from sandbox_science.contracts.lockfile_diffs import LockfileDiffPage
from sandbox_science.contracts.provision_observations import (
    ProvisionDetail,
    ProvisionFilePage,
    ProvisionObservation,
    RevisionProbePage,
    RevisionSummaryPage,
)
from sandbox_science.contracts.revision_packages import RevisionPackagePage
from sandbox_science.orchestrator.accounting import UsageAccount
from sandbox_science.orchestrator.connector_settings import ConnectorSettings
from sandbox_science.orchestrator.context.history.captured import (
    CapturedCalls,
    ContextParts,
    ContextText,
)
from sandbox_science.orchestrator.continuity.resources import ResourceRequestPage
from sandbox_science.orchestrator.continuity.status import StatusPage
from sandbox_science.orchestrator.documents.annotations import CHANGE as COMMENT_CHANGE
from sandbox_science.orchestrator.memory import MemoryChange
from sandbox_science.orchestrator.model_settings import ModelSettings
from sandbox_science.orchestrator.project_grants import GrantListing
from sandbox_science.orchestrator.runtree import RunTree
from sandbox_science.orchestrator.uploads import UploadPage
from sandbox_science.workspace.agent_messages import AgentMessagePage
from sandbox_science.workspace.api import ProjectAnswer, ProjectListing, SessionDetail
from sandbox_science.workspace.approvals import AnswerResult, PendingListing
from sandbox_science.workspace.artifacts import (
    ArtifactListing,
    ChartLink,
    FigureLink,
    RevisionListing,
    ViewerLink,
)
from sandbox_science.workspace.chat import (
    EventPage,
    MessageAnswer,
    MessageListing,
    QuestionAnswer,
    QuestionListing,
    Requested,
)
from sandbox_science.workspace.classifications import (
    ClassificationListing,
    ClassificationView,
    DraftListing,
)
from sandbox_science.workspace.context_captures import IndexStatus
from sandbox_science.workspace.context_inspector import (
    ContextAttemptsPage,
    ContextHistoryPage,
    ContextTimelinePage,
)
from sandbox_science.workspace.domain_contracts import (
    DOCUMENT_CHANGE,
    BackgroundJob,
    CommentChange,
    CommentListing,
    CommentThread,
    DocumentChange,
    DocumentHistory,
    DocumentListing,
    DocumentRevision,
    JobCancellation,
    JobListing,
    JobRelease,
    PatchPreview,
)
from sandbox_science.workspace.frontier import (
    FrontierAnswer,
    FrontierCategories,
    FrontierCategory,
    FrontierHistoryAnswer,
    FrontierPastAnswer,
)
from sandbox_science.workspace.history import HistoryChunk, HistoryPage
from sandbox_science.workspace.kernels import WorkspaceObservation
from sandbox_science.workspace.ledger import (
    FoldProposalList,
    LedgerCitations,
    LedgerEntry,
    LedgerGate,
    LedgerHistory,
    LedgerListing,
    LedgerWork,
)
from sandbox_science.workspace.mcp_services import McpInventory
from sandbox_science.workspace.memory import MemoryAnswer, MemoryHistory, MemoryProposals
from sandbox_science.workspace.messages import ToolRecordChunk, ToolRecordListing
from sandbox_science.workspace.monitor import ProjectMonitor
from sandbox_science.workspace.plan_edits import BodyChange, EditReceipt
from sandbox_science.workspace.plans import WorkspacePlan
from sandbox_science.workspace.skills import ProjectSkills, SkillSettingsChange, SkillSettingsView

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "workspace" / "src" / "lib" / "api-types.ts"
MODELS: tuple[type[BaseModel], ...] = (
    UploadPage,
    StatusPage,
    ResourceRequestPage,
    UsageAccount,
    HistoryChunk,
    HistoryPage,
    CapturedCalls,
    ContextParts,
    ContextText,
    IndexStatus,
    ContextHistoryPage,
    ContextAttemptsPage,
    ContextTimelinePage,
    ToolRecordChunk,
    ToolRecordListing,
    FigureLink,
    ChartLink,
    ProjectListing,
    ProjectAnswer,
    SessionDetail,
    RunTree,
    AgentMessagePage,
    WorkspaceObservation,
    WorkspacePlan,
    BodyChange,
    EditReceipt,
    ProvisionObservation,
    ProvisionDetail,
    ProvisionFilePage,
    RevisionSummaryPage,
    RevisionProbePage,
    LockfileDiffPage,
    RevisionPackagePage,
    MessageAnswer,
    MessageListing,
    EventPage,
    QuestionListing,
    QuestionAnswer,
    Requested,
    GrantListing,
    AnswerResult,
    PendingListing,
    ConnectorSettings,
    ArtifactListing,
    RevisionListing,
    ViewerLink,
    LedgerListing,
    LedgerEntry,
    LedgerCitations,
    LedgerHistory,
    LedgerWork,
    LedgerGate,
    FrontierCategories,
    FrontierCategory,
    FrontierAnswer,
    FrontierHistoryAnswer,
    FrontierPastAnswer,
    MemoryAnswer,
    ProjectSkills,
    McpInventory,
    ProjectMonitor,
    ClassificationListing,
    ClassificationView,
    DraftListing,
    ClassificationAccepted,
    DriftAccepted,
    FoldProposalList,
    SkillSettingsChange,
    SkillSettingsView,
    MemoryProposals,
    MemoryHistory,
    MemoryChange,
    ModelSettings,
)
WIRE_TYPES: dict[str, TypeAdapter[Any]] = {
    model.__name__: TypeAdapter(model)
    for model in (
        DocumentListing,
        DocumentRevision,
        DocumentHistory,
        DocumentChange,
        PatchPreview,
        CommentListing,
        CommentThread,
        CommentChange,
        JobListing,
        JobCancellation,
        JobRelease,
        BackgroundJob,
    )
}
REQUESTS: dict[str, TypeAdapter[Any]] = {
    "DocumentChangeRequest": DOCUMENT_CHANGE,
    "CommentChangeRequest": COMMENT_CHANGE,
}

HEADER = (
    "// SPDX-License-Identifier: MIT\n"
    "// Generated by scripts/generate_api_types.py from the workspace's API schemas.\n"
    "// Do not edit.\n"
)

Schema = dict[str, Any]
IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def _union(parts: list[str]) -> str:
    unique = list(dict.fromkeys(parts))
    return " | ".join(unique) if unique else "never"


def _wrap(text: str) -> str:
    return f"({text})" if " | " in text else text


def _type(schema: Schema, *, serialized: bool = True) -> str:
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", 1)[-1]
    if "const" in schema:
        return json.dumps(schema["const"])
    if "enum" in schema:
        return _union([json.dumps(value) for value in schema["enum"]])
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return _union([_type(part, serialized=serialized) for part in schema[key]])
    if "allOf" in schema and len(schema["allOf"]) == 1:
        return _type(schema["allOf"][0], serialized=serialized)
    kind = schema.get("type")
    if isinstance(kind, list):
        return _union([_type({**schema, "type": one}, serialized=serialized) for one in kind])
    match kind:
        case "string":
            return "string"
        case "integer" | "number":
            return "number"
        case "boolean":
            return "boolean"
        case "null":
            return "null"
        case "array":
            if "prefixItems" in schema:
                items = ", ".join(
                    _type(item, serialized=serialized) for item in schema["prefixItems"]
                )
                return f"[{items}]"
            return f"{_wrap(_type(schema.get('items', {}), serialized=serialized))}[]"
        case "object":
            if "properties" in schema:
                return _object(schema, "", serialized=serialized)
            extra = schema.get("additionalProperties")
            value = _type(extra, serialized=serialized) if isinstance(extra, dict) else "unknown"
            return f"Record<string, {value}>"
        case _:
            return "unknown"


def _object(schema: Schema, indent: str, *, serialized: bool = True) -> str:
    # Model answers carry defaults; request and dictionary schemas retain omissions.
    lines = ["{"]
    for name, field in schema["properties"].items():
        key = name if IDENTIFIER.fullmatch(name) else json.dumps(name)
        optional = "" if serialized or name in schema.get("required", []) else "?"
        lines.append(f"{indent}  {key}{optional}: {_type(field, serialized=serialized)};")
    lines.append(f"{indent}}}")
    return "\n".join(lines)


def _comment(schema: Schema) -> str:
    # A docstring's own "*/" must not end the comment early.
    text = " ".join(str(schema.get("description", "")).split()).replace("*/", "*\\/")
    return f"/** {text} */\n" if text else ""


def _request_names(schema: Schema, names: dict[str, str]) -> Schema:
    """Namespace request definitions without changing aliases or required fields."""

    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        result: Schema = {}
        for key, item in value.items():
            if key == "$defs":
                result[key] = {names[name]: visit(definition) for name, definition in item.items()}
            elif key == "$ref" and item.startswith("#/$defs/"):
                result[key] = "#/$defs/" + names[item.rsplit("/", 1)[-1]]
            else:
                result[key] = visit(item)
        return result

    return visit(schema)


def generate() -> str:
    # Model responses dump field names; requests use their validation aliases.
    _, top = models_json_schema(
        [(model, "serialization") for model in MODELS],
        by_alias=False,
        ref_template="#/$defs/{model}",
    )
    # Two models of one name get module-qualified names, which no one means to import.
    clashes = sorted(name for name in top["$defs"] if "__" in name)
    if clashes:
        raise SystemExit(f"two response models share a name; rename one: {', '.join(clashes)}")
    schemas = {name: (schema, True) for name, schema in top["$defs"].items()}
    if WIRE_TYPES.keys() & REQUESTS.keys():
        raise SystemExit("wire responses and requests share a root name")
    for name, adapter in (WIRE_TYPES | REQUESTS).items():
        schema = adapter.json_schema(mode="validation", by_alias=True)
        if name in REQUESTS:
            # Validation and serialization can give a shared Python model different
            # shapes. Give request definitions a stable domain prefix, including refs.
            prefix = name.removesuffix("ChangeRequest") + "Request"
            names = {key: prefix + key for key in schema.get("$defs", {})}
            schema = _request_names(schema, names)
        definitions = schema.pop("$defs", {})
        if schema != {"$ref": f"#/$defs/{name}"}:
            definitions[name] = schema
        for key, definition in definitions.items():
            if key in schemas and schemas[key][0] != definition:
                raise SystemExit(f"API schemas share a name with different shapes: {key}")
            schemas.setdefault(key, (definition, False))
    parts = [HEADER]
    for name, (schema, serialized) in sorted(schemas.items()):
        parts.append("\n" + _comment(schema))
        if schema.get("type") == "object" and "properties" in schema:
            parts.append(f"export interface {name} {_object(schema, '', serialized=serialized)}\n")
        else:
            parts.append(f"export type {name} = {_type(schema, serialized=serialized)};\n")
    return "".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the browser's API types.")
    parser.add_argument("--check", action="store_true", help="fail when the file is stale")
    check = parser.parse_args().check
    text = generate()
    if check:
        if not OUT.is_file() or OUT.read_text(encoding="utf-8") != text:
            print(f"{OUT.relative_to(ROOT)} is stale: run scripts/generate_api_types.py")
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
