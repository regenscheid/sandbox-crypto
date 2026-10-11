# SPDX-License-Identifier: MIT
"""The capability catalog: capabilities, their conda-forge packages and probes (CP-3).

The catalog is a reviewed data file in the repository, ``capabilities.toml`` beside this
module, which only the platform extends for now (open question 6 of the Conda
provisioning spec). Each operation records the catalog's version and the SHA-256 of the
file it used. A probe is an ordinary shell execution in the target environment; it
passes only on its expected output (CP-21, CP-22).
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from sandbox_science.contracts import CatalogRef
from sandbox_science.contracts.provisioning import CondaSpec
from sandbox_science.contracts.revisions import CapabilityName, ProbeId

CATALOG: Final = Path(__file__).with_name("capabilities.toml")
MAX_CATALOG_BYTES: Final = 1024 * 1024


class CatalogError(Exception):
    """The catalog file cannot be read, or does not validate."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProbeSpec(_Model):
    """A probe: a command, and the output that shows the capability works."""

    id: ProbeId
    version: Annotated[int, Field(ge=1)] = 1
    kind: Literal["functional", "import_check"] = "functional"
    argv: tuple[Annotated[str, StringConstraints(min_length=1, max_length=4096)], ...] = Field(
        min_length=1, max_length=32
    )
    stdout: Annotated[str, StringConstraints(max_length=4096)] | None = None
    pattern: Annotated[str, StringConstraints(min_length=1, max_length=1024)] | None = None
    timeout_seconds: Annotated[float, Field(gt=0, le=3600)] = 120.0

    @model_validator(mode="after")
    def _one_expectation(self) -> Self:
        if (self.stdout is None) == (self.pattern is None):
            raise ValueError(f"probe {self.id}: give exactly one of stdout and pattern")
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"probe {self.id}: pattern does not compile: {exc}") from exc
        return self

    def passes(self, stdout: str) -> bool:
        """Whether a run's standard output is the one expected."""
        text = normalize_output(stdout)
        if self.stdout is not None:
            return text == normalize_output(self.stdout)
        return re.fullmatch(self.pattern or "", text) is not None


class CapabilitySpec(_Model):
    description: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    conda: tuple[CondaSpec, ...] = Field(min_length=1, max_length=32)
    probes: tuple[ProbeSpec, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _unique_probes(self) -> Self:
        ids = [probe.id for probe in self.probes]
        if len(set(ids)) != len(ids):
            raise ValueError("probe IDs must be unique within a capability")
        return self


class _File(_Model):
    version: Annotated[int, Field(ge=1)]
    capabilities: dict[CapabilityName, CapabilitySpec] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class Catalog:
    """The catalog as loaded: its version, the file's SHA-256, and the capabilities."""

    version: int
    sha256: str
    capabilities: dict[str, CapabilitySpec]

    @property
    def ref(self) -> CatalogRef:
        return CatalogRef(version=self.version, sha256=self.sha256)

    def get(self, name: str) -> CapabilitySpec | None:
        return self.capabilities.get(name)

    @classmethod
    def parse(cls, data: bytes, source: str = "the catalog") -> Catalog:
        """Validate a catalog file's bytes; raise CatalogError."""
        if len(data) > MAX_CATALOG_BYTES:
            raise CatalogError(f"{source} is larger than {MAX_CATALOG_BYTES} bytes")
        try:
            document = tomllib.loads(data.decode("utf-8"))
            parsed = _File.model_validate(document)
        except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
            raise CatalogError(f"{source} is not a valid catalog: {exc}") from exc
        return cls(parsed.version, hashlib.sha256(data).hexdigest(), dict(parsed.capabilities))


def load_catalog(path: Path = CATALOG) -> Catalog:
    """Read and validate the catalog file; raise CatalogError."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise CatalogError(f"cannot read {path}: {exc}") from exc
    return Catalog.parse(data, str(path))


def normalize_output(text: str) -> str:
    """Text as probes compare it: line ends as ``\\n``, trailing whitespace removed from each
    line and from the end."""
    lines = text.replace("\r\n", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).rstrip()
