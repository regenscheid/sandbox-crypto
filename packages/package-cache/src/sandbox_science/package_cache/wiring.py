# SPDX-License-Identifier: MIT
"""Where the ecosystem adapters are wired to the settings.

``build_adapters`` creates one adapter per ecosystem whose settings table is
present (``[pypi]``, ``[conda]``, ``[cargo]``, ``[go]``, ``[npm]``, ``[opam]``,
``[hackage]``), keyed by the adapter's prefix. An ecosystem without a table
gets no adapter, so its paths answer 404.
"""

from __future__ import annotations

from .adapter import Adapter
from .adapters.cargo import CargoAdapter
from .adapters.conda import CondaAdapter
from .adapters.go import GoAdapter
from .adapters.hackage import HackageAdapter
from .adapters.npm import NpmAdapter
from .adapters.opam import OpamAdapter
from .adapters.pypi import PypiAdapter
from .settings import Settings


def build_adapters(settings: Settings) -> dict[str, Adapter]:
    """Return the adapters for the ecosystems ``settings`` enables, keyed by prefix."""
    public_url = settings.public_url
    adapters: list[Adapter] = []
    if settings.pypi is not None:
        adapters.append(
            PypiAdapter(index=settings.pypi.index, files=settings.pypi.files, public_url=public_url)
        )
    if settings.conda is not None:
        adapters.append(
            CondaAdapter(
                base=settings.conda.base,
                channels=settings.conda.channels,
                subdirs=settings.conda.subdirs,
            )
        )
    if settings.cargo is not None:
        adapters.append(
            CargoAdapter(
                index=settings.cargo.index,
                download=settings.cargo.download,
                public_url=public_url,
            )
        )
    if settings.go is not None:
        adapters.append(GoAdapter(proxy=settings.go.proxy, sumdb=settings.go.sumdb))
    if settings.npm is not None:
        adapters.append(NpmAdapter(registry=settings.npm.registry, public_url=public_url))
    if settings.opam is not None:
        adapters.append(OpamAdapter(repository=settings.opam.repository))
    if settings.hackage is not None:
        adapters.append(HackageAdapter(repository=settings.hackage.repository))
    return {adapter.prefix: adapter for adapter in adapters}
