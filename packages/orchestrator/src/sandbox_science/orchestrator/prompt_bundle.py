# SPDX-License-Identifier: MIT
"""Exact MAIN system/tool input pins. These identify content, never authority."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self, cast

from pydantic import Field, model_validator

from sandbox_science.contracts.model import Contract, Identifier

from .model import ToolSpec
from .stock_prompts import MAX_TEMPLATE_BYTES, StockPrompt, StockTemplateId

ASSEMBLY_VERSION = "sandbox-main-bundle-1"
BASE_VERSION = "sandbox-main-2"
MAX_SYSTEM_BYTES = 64 * 1024
MAX_SCHEMA_BYTES = 512 * 1024
MAX_PROVENANCE_BYTES = 16 * 1024
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Size = Annotated[int, Field(strict=True, ge=0, le=MAX_SCHEMA_BYTES)]


def _json(value: object, *, canonical: bool = True) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=canonical, separators=(",", ":")
    ).encode("utf-8")


def _sha(domain: bytes, value: bytes) -> str:
    return hashlib.sha256(domain + value).hexdigest()


class ContentPin(Contract):
    bytes: Size
    sha256: Digest


class Component(ContentPin):
    component: Literal["main", "custom", "planning"]
    version: Identifier | None

    @model_validator(mode="after")
    def _version(self) -> Self:
        if (self.component == "custom") != (self.version is None):
            raise ValueError("only custom content has no catalog version")
        return self


class ToolPin(ContentPin):
    names: tuple[Identifier, ...] = Field(max_length=128)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.names)) != len(self.names):
            raise ValueError("duplicate offered tool name")
        return self


class CompleteProvenance(Contract):
    status: Literal["complete"] = "complete"
    assembly_version: Literal["sandbox-main-bundle-1"] = ASSEMBLY_VERSION
    role: Literal["main"] = "main"
    components: tuple[Component, ...] = Field(min_length=1, max_length=2)
    system: ContentPin
    tools: ToolPin
    bundle_sha256: Digest

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if self.components[0].component not in {"main", "custom"} or (
            len(self.components) == 2 and self.components[1].component != "planning"
        ):
            raise ValueError("invalid MAIN component order")
        if self.system.bytes > MAX_SYSTEM_BYTES or self.system.bytes != sum(
            component.bytes for component in self.components
        ) + 2 * (len(self.components) - 1):
            raise ValueError("invalid rendered system byte accounting")
        fields = self.model_dump(mode="json", exclude={"bundle_sha256"})
        if _sha(b"main-prompt-bundle-v1\0", _json(fields)) != self.bundle_sha256:
            raise ValueError("invalid prompt bundle digest")
        if len(self.model_dump_json().encode()) > MAX_PROVENANCE_BYTES:
            raise ValueError("prompt provenance exceeds its budget")
        return self


class UnavailableProvenance(Contract):
    status: Literal["unavailable"] = "unavailable"
    assembly_version: Literal["sandbox-main-bundle-1"] = ASSEMBLY_VERSION
    role: Literal["main"] = "main"
    reason: Literal["system_too_large", "system_not_utf8", "metadata_too_large"]


LegacyProvenance = Annotated[
    CompleteProvenance | UnavailableProvenance, Field(discriminator="status")
]


class StockComponent(ContentPin):
    component: Literal["stock"] = "stock"
    template_id: StockTemplateId
    version: Identifier

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if (
            not 1 <= self.bytes <= MAX_TEMPLATE_BYTES
            or re.fullmatch(rf"sandbox-stock-{self.template_id}-[1-9][0-9]{{0,8}}", self.version)
            is None
        ):
            raise ValueError("invalid retained research template pin")
        return self


SelectedComponent = Annotated[Component | StockComponent, Field(discriminator="component")]


class SelectedProvenance(Contract):
    """Complete selected assembly; v1 serialization and parsing remain separate."""

    status: Literal["complete"] = "complete"
    assembly_version: Literal["sandbox-main-bundle-2"] = "sandbox-main-bundle-2"
    role: Literal["main"] = "main"
    components: tuple[SelectedComponent, ...] = Field(min_length=2, max_length=3)
    system: ContentPin
    tools: ToolPin
    bundle_sha256: Digest

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if (
            self.components[0].component != "main"
            or self.components[1].component != "stock"
            or (len(self.components) == 3 and self.components[2].component != "planning")
        ):
            raise ValueError("invalid selected MAIN component order")
        if self.system.bytes > MAX_SYSTEM_BYTES or self.system.bytes != sum(
            component.bytes for component in self.components
        ) + 2 * (len(self.components) - 1):
            raise ValueError("invalid selected system byte accounting")
        fields = self.model_dump(mode="json", exclude={"bundle_sha256"})
        if _sha(b"main-prompt-bundle-v2\0", _json(fields)) != self.bundle_sha256:
            raise ValueError("invalid selected prompt bundle digest")
        if len(self.model_dump_json().encode()) > MAX_PROVENANCE_BYTES:
            raise ValueError("selected prompt provenance exceeds its budget")
        return self


# The legacy branch must still accept omitted v1 default fields. A new outer
# discriminator would require assembly_version and reject those existing inputs.
PromptProvenance = LegacyProvenance | SelectedProvenance


def _text(text: str) -> bytes:
    if len(text) > MAX_SYSTEM_BYTES:
        raise OverflowError
    raw = text.encode("utf-8")
    if len(raw) > MAX_SYSTEM_BYTES:
        raise OverflowError
    return raw


def verify_retained(provenance: PromptProvenance, first: object) -> None:
    """Check recorded content without reconstructing historical versions from today."""
    if not isinstance(provenance, (CompleteProvenance, SelectedProvenance)):
        return
    if not isinstance(first, dict):
        raise ValueError("prompt provenance requires its recorded system message")
    message = cast(dict[str, object], first)
    if message.get("role") != "system":
        raise ValueError("prompt provenance requires its recorded system message")
    text = message.get("content")
    if not isinstance(text, str):
        raise ValueError("recorded system content is not text")
    try:
        raw = _text(text)
    except (OverflowError, UnicodeError):
        raise ValueError("recorded system is outside the complete provenance bounds") from None
    if len(raw) != provenance.system.bytes or _sha(b"", raw) != provenance.system.sha256:
        raise ValueError("recorded system does not match prompt provenance")
    offset = 0
    for index, component in enumerate(provenance.components):
        if index:
            if raw[offset : offset + 2] != b"\n\n":
                raise ValueError("recorded component separator changed")
            offset += 2
        part = raw[offset : offset + component.bytes]
        if _sha(b"", part) != component.sha256:
            raise ValueError("recorded component does not match prompt provenance")
        offset += component.bytes


def _tool_bytes(specs: Sequence[ToolSpec]) -> tuple[bytes, bytes, tuple[str, ...]]:
    """Preflight before encoding, preserving wire object order in the owned copy."""
    if len(specs) > 128:
        raise ValueError("too many offered tools")
    budget, nodes = 0, 0

    def walk(value: Any, depth: int = 0) -> None:
        nonlocal budget, nodes
        nodes += 1
        budget += 8
        if depth > 32 or nodes > 32768:
            raise ValueError("tool schema structure exceeds its budget")
        if isinstance(value, str):
            if len(value) > MAX_SCHEMA_BYTES:
                raise ValueError("tool schema text exceeds its budget")
            budget += len(value.encode("utf-8")) + sum(
                5 if ord(character) < 32 else 1 if character in {'"', "\\"} else 0
                for character in value
            )
        elif value is None or type(value) is bool:
            pass
        elif type(value) is int:
            if value.bit_length() > 4096:
                raise ValueError("tool schema integer exceeds its budget")
            budget += value.bit_length() // 3 + 2
        elif type(value) is float:
            if not math.isfinite(value):
                raise ValueError("nonfinite tool schema number")
            budget += 32
        elif isinstance(value, (dict, list, tuple)):
            if len(cast(Sequence[object] | dict[object, object], value)) > 4096:
                raise ValueError("tool schema container exceeds its budget")
            if isinstance(value, dict):
                for key, child in cast(dict[object, object], value).items():
                    if not isinstance(key, str):
                        raise ValueError("tool schema keys must be text")
                    walk(key, depth + 1)
                    walk(child, depth + 1)
            else:
                for child in cast(Sequence[object], value):
                    walk(child, depth + 1)
        else:
            raise ValueError("tool schema contains a non-JSON value")
        if budget > MAX_SCHEMA_BYTES:
            raise ValueError("tool schema bytes exceed their budget")

    values: list[dict[str, object]] = []
    names: list[str] = []
    for spec in specs:
        name = cast(object, spec.name)
        description = cast(object, spec.description)
        parameters = cast(object, spec.parameters)
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) is None
        ):
            raise ValueError("invalid offered tool name")
        if not isinstance(description, str) or not isinstance(parameters, dict):
            raise ValueError("invalid offered tool description/schema")
        names.append(spec.name)
        values.append(
            {"name": spec.name, "description": spec.description, "parameters": spec.parameters}
        )
    if len(names) != len(set(names)):
        raise ValueError("duplicate offered tool name")
    walk(values)
    canonical, owned = _json(values), _json(values, canonical=False)
    if max(len(canonical), len(owned)) > MAX_SCHEMA_BYTES:
        raise ValueError("offered tool encoding exceeds its budget")
    return owned, canonical, tuple(names)


@dataclass(frozen=True, slots=True)
class MainPromptBundle:
    system: str
    provenance: PromptProvenance
    _owned_tools: bytes
    _tools_sha256: str

    def tool_specs(self) -> tuple[ToolSpec, ...]:
        """Fresh per-call schemas; no model can mutate the stored owned projection."""
        return tuple(ToolSpec(**item) for item in json.loads(self._owned_tools))

    def verify(self, first: object, passed: Sequence[ToolSpec]) -> None:
        if not isinstance(first, dict):
            raise ValueError("actual MAIN system input changed")
        message = cast(dict[str, object], first)
        if message.get("role") != "system" or message.get("content") != self.system:
            raise ValueError("actual MAIN system input changed")
        _, canonical, _ = _tool_bytes(passed)
        if _sha(b"main-tool-schemas-v1\0", canonical) != self._tools_sha256:
            raise ValueError("actual MAIN tool input changed")
        verify_retained(self.provenance, message)


def assemble_main(
    system: str,
    specs: Sequence[ToolSpec],
    *,
    builtin_content: bool,
    guidance: tuple[str, str] | None = None,
    stock: StockPrompt | None = None,
) -> MainPromptBundle:
    """Recognized builtin bytes identify content, not who supplied it or permissions."""
    if stock is not None:
        return _assemble_selected(system, specs, builtin_content, guidance, stock)
    owned, canonical, names = _tool_bytes(specs)
    try:
        if builtin_content:
            _text(system)
        if guidance is not None:
            _text(guidance[1])
    except (OverflowError, UnicodeError):
        raise ValueError("installed MAIN prompt content exceeds its bounds") from None
    tool_sha = _sha(b"main-tool-schemas-v1\0", canonical)
    rendered = system + ("\n\n" + guidance[1] if guidance is not None else "")
    provenance: PromptProvenance
    try:
        raw = _text(rendered)
        components = [
            Component(
                component="main" if builtin_content else "custom",
                version=BASE_VERSION if builtin_content else None,
                bytes=len(_text(system)),
                sha256=_sha(b"", _text(system)),
            )
        ]
        if guidance is not None:
            part = _text(guidance[1])
            components.append(
                Component(
                    component="planning",
                    version=guidance[0],
                    bytes=len(part),
                    sha256=_sha(b"", part),
                )
            )
        fields = {
            "status": "complete",
            "assembly_version": ASSEMBLY_VERSION,
            "role": "main",
            "components": [c.model_dump(mode="json") for c in components],
            "system": {"bytes": len(raw), "sha256": _sha(b"", raw)},
            "tools": {"bytes": len(canonical), "sha256": tool_sha, "names": names},
        }
        if len(_json(fields)) + 100 > MAX_PROVENANCE_BYTES:
            if builtin_content:
                raise ValueError("installed MAIN prompt metadata exceeds its bounds")
            provenance = UnavailableProvenance(reason="metadata_too_large")
        else:
            provenance = CompleteProvenance.model_validate(
                {**fields, "bundle_sha256": _sha(b"main-prompt-bundle-v1\0", _json(fields))}
            )
    except OverflowError:
        if builtin_content:
            raise ValueError("installed MAIN prompt assembly exceeds its bounds") from None
        provenance = UnavailableProvenance(reason="system_too_large")
    except UnicodeError:
        provenance = UnavailableProvenance(reason="system_not_utf8")
    return MainPromptBundle(rendered, provenance, owned, tool_sha)


def _assemble_selected(
    system: str,
    specs: Sequence[ToolSpec],
    builtin_content: bool,
    guidance: tuple[str, str] | None,
    stock: StockPrompt,
) -> MainPromptBundle:
    if not builtin_content:
        raise ValueError("research templates require the builtin MAIN base")
    stock.validate()
    try:
        base, selected = _text(system), _text(stock.text)
        planning = _text(guidance[1]) if guidance is not None else None
        rendered = "\n\n".join([system, stock.text] + ([guidance[1]] if guidance else []))
        raw = _text(rendered)
    except (OverflowError, UnicodeError):
        raise ValueError("installed selected MAIN content exceeds its bounds") from None
    owned, canonical, names = _tool_bytes(specs)
    tool_sha = _sha(b"main-tool-schemas-v1\0", canonical)
    components: list[Component | StockComponent] = [
        Component(component="main", version=BASE_VERSION, bytes=len(base), sha256=_sha(b"", base)),
        StockComponent(
            template_id=stock.template_id,
            version=stock.version,
            bytes=len(selected),
            sha256=_sha(b"", selected),
        ),
    ]
    if guidance is not None and planning is not None:
        components.append(
            Component(
                component="planning",
                version=guidance[0],
                bytes=len(planning),
                sha256=_sha(b"", planning),
            )
        )
    fields = {
        "status": "complete",
        "assembly_version": "sandbox-main-bundle-2",
        "role": "main",
        "components": [component.model_dump(mode="json") for component in components],
        "system": {"bytes": len(raw), "sha256": _sha(b"", raw)},
        "tools": {"bytes": len(canonical), "sha256": tool_sha, "names": names},
    }
    if len(_json(fields)) + 100 > MAX_PROVENANCE_BYTES:
        raise ValueError("installed selected MAIN metadata exceeds its bounds")
    provenance = SelectedProvenance.model_validate(
        {**fields, "bundle_sha256": _sha(b"main-prompt-bundle-v2\0", _json(fields))}
    )
    return MainPromptBundle(rendered, provenance, owned, tool_sha)
