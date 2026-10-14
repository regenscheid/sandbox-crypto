# SPDX-License-Identifier: MIT
"""Selected retained package metadata; no installed-inventory or license authority."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .lockfile_diffs import (
    Bounded,
    HoldReason,
    LicenseObservation,
    LockfileHeader,
    LockfileSelection,
    Name,
)
from .provision_observations import Counter, Observation, PageLimit
from .revisions import CondaPackage, PipDistribution


class RevisionPackage(Bounded):
    """One recorded package and its declared license, including unchanged packages."""

    ecosystem: Literal["conda", "pip"]
    name: Name
    location: Literal["prefix", "user"] | None
    package: CondaPackage | PipDistribution
    license: LicenseObservation

    @model_validator(mode="after")
    def _entry(self) -> Self:
        if self.ecosystem == "conda":
            if (
                not isinstance(self.package, CondaPackage)
                or self.location is not None
                or self.name != self.package.name
                or self.license.declared != self.package.license
            ):
                raise ValueError("Conda identity or license does not bind its package")
        elif (
            not isinstance(self.package, PipDistribution)
            or self.location != self.package.location
            or self.name != re.sub(r"[-_.]+", "-", self.package.name).lower()
            or self.license.declared != self.package.license
        ):
            raise ValueError("pip identity or declared license does not bind its package")
        return self


class RevisionPackagePage(Observation):
    """A stateless page of one exactly selected retained lockfile's recorded packages."""

    selection: LockfileSelection
    status: Literal["complete", "held"]
    holds: tuple[HoldReason, ...] = Field(max_length=1)
    header: LockfileHeader | None
    total: Annotated[int, Field(strict=True, ge=0, le=40_000)] | None
    offset: Annotated[int, Field(strict=True, ge=0, le=40_000)]
    limit: PageLimit
    next_offset: Counter | None
    packages: tuple[RevisionPackage, ...] = Field(max_length=8)

    @model_validator(mode="after")
    def _page(self) -> Self:
        if self.header is not None:
            ref = self.selection.lockfile
            if ref is None or (
                self.header.sha256,
                self.header.size,
                self.header.conda_packages,
                self.header.pip_distributions,
            ) != (ref.sha256, ref.size, ref.conda_packages, ref.pip_distributions):
                raise ValueError("header does not bind selected input")
        if self.status == "held":
            if (
                not self.holds
                or self.total is not None
                or self.next_offset is not None
                or self.packages
            ):
                raise ValueError("held input cannot claim a package inventory")
        else:
            if self.holds or self.header is None or self.header.skipped:
                raise ValueError("complete inventory needs a proved skipped-zero header")
            if self.total != self.header.conda_packages + self.header.pip_distributions:
                raise ValueError("total does not bind selected header counts")
            if self.total is None or self.offset > self.total:
                raise ValueError("package offset exceeds selected total")
            end = self.offset + len(self.packages)
            if (
                len(self.packages) > self.limit
                or end > self.total
                or self.next_offset != (end if end < self.total else None)
                or (self.next_offset is not None and not self.packages)
            ):
                raise ValueError("package continuation mismatch")
            keys = tuple((row.ecosystem, row.location or "", row.name) for row in self.packages)
            if keys != tuple(sorted(set(keys))):
                raise ValueError("package page needs unique canonical ordering")
            if any(
                sum(row.ecosystem == kind for row in self.packages) > count
                for kind, count in (
                    ("conda", self.header.conda_packages),
                    ("pip", self.header.pip_distributions),
                )
            ):
                raise ValueError("package page exceeds selected ecosystem counts")
        return self
