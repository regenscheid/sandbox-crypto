# SPDX-License-Identifier: MIT
"""Selected retained package metadata; no installed-inventory or license authority."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .lockfile_diffs import (
    HoldReason,
    LicenseObservation,
    LockfileHeader,
    LockfileSelection,
    Name,
)
from .model import Counter, StrictContract, check_page
from .provision_observations import MAX_OBSERVATION_PAGE, Observation, PageLimit
from .revisions import MAX_LOCKFILE_ENTRIES, CondaPackage, PipDistribution, pep503_name

MAX_PACKAGES = 2 * MAX_LOCKFILE_ENTRIES
"""Packages one lockfile records: its Conda and its pip entries."""


class RevisionPackage(StrictContract):
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
            or self.name != pep503_name(self.package.name)
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
    total: Annotated[int, Field(strict=True, ge=0, le=MAX_PACKAGES)] | None
    offset: Annotated[int, Field(strict=True, ge=0, le=MAX_PACKAGES)]
    limit: PageLimit
    next_offset: Counter | None
    packages: tuple[RevisionPackage, ...] = Field(max_length=MAX_OBSERVATION_PAGE)

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
            if self.total is None:
                raise ValueError("complete inventory needs its total")
            check_page(self.offset, len(self.packages), self.limit, self.total, self.next_offset)
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
