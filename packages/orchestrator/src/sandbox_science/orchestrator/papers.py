# SPDX-License-Identifier: MIT
"""One paper under its identifiers, whichever server read it (LG-15, CR-4, CM-12a).

The ledger names a reference's paper by schemes (``doi``, ``eprint``, ``arxiv``,
``nist``, ``isbn``, ``url``); the e-print server prints an ePrint's identifier in brackets
(``[2024/1234]``); the library server (crypto-research-mcp's ``library-mcp``) names each
paper by a work label of its own, chosen once from the paper's first identifiers:
``eprint/2024/1234``, ``doi:10.1007/…``, ``arxiv:2401.01234``, ``nist:ir-8610``, and for
a paper known by nothing better ``openalex:W…``, ``zbmath:…``, ``dblp:…``, ``url:…`` or
``upload:…``. This module maps each onto the others, so that a reference recorded with
``eprint: 2024/1234`` is the paper a library read of ``eprint/2024/1234`` returned, and a
paper read partly through either server is one paper to the ledger and the register.

Values are compared in a normal form: an ePrint as ``YYYY/NNN`` (at least three digits,
as the archive writes them), a DOI in lower case without its resolver, an arXiv ID
without its version, in lower case, a NIST designation as ``nist_key`` writes it (the
read register's key). A label in another scheme names no ledger identifier: a quote of
it confirms nothing.
"""

from __future__ import annotations

import re
from typing import Final

LEDGER_SCHEMES: Final = ("eprint", "doi", "arxiv", "nist")
"""The ledger's schemes a library work label can name: a reference's identifier in one
of these is what a library quote is confirmed against."""

_EPRINT: Final = re.compile(
    r"^(?:eprint[/:]|https?://eprint\.iacr\.org/)?(\d{4})/(\d{1,5})(?:\.pdf)?/?$", re.I
)
_DOI: Final = re.compile(r"^(?:doi:|https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/\S+)$", re.I)
_ARXIV: Final = re.compile(
    r"^(?:arxiv:|https?://(?:export\.|www\.)?arxiv\.org/(?:abs|pdf|html)/)?"
    r"(\d{4}\.\d{4,5}|[a-z.-]+/\d{7})(?:v\d+)?(?:\.pdf)?$",
    re.I,
)
_LABEL: Final = re.compile(r"^(?:eprint/(\d{4}/\d{1,5})|(doi|arxiv|nist):(\S{1,500}))$")


def nist_key(designation: str) -> str:
    """A NIST publication's designation in its normal form: ``FIPS 203``,
    ``NIST.FIPS.203``, ``fips.203`` and ``fips203`` alike, and ``SP 800-56A Rev. 3`` as
    ``SP80056AR3``."""
    found = designation.strip().upper().removeprefix("NIST").lstrip(". ")
    found = re.sub(r"\s*REV(?:ISION)?\b\.?\s*", "R", found)
    return re.sub(r"[\s._-]+", "", found)


def normal(scheme: str, value: str) -> str | None:
    """``value``, an identifier in ledger scheme ``scheme``, in its normal form; None
    when it is not one of that scheme, or the scheme is none a library label names."""
    value = value.strip()
    if scheme == "eprint":
        found = _EPRINT.match(value)
        return None if found is None else f"{found.group(1)}/{int(found.group(2)):03d}"
    if scheme == "doi":
        found = _DOI.match(value)
        return None if found is None else found.group(1).casefold().rstrip(".,;")
    if scheme == "arxiv":
        found = _ARXIV.match(value)
        return None if found is None else found.group(1).casefold()
    if scheme == "nist":
        key = nist_key(re.sub(r"^nist:", "", value, flags=re.I))
        return key or None
    return None


def library_paper(label: str) -> tuple[str, str] | None:
    """The ledger identifier, as (scheme, normal value), that library work label
    ``label`` names: ``eprint/2024/1234`` is the ePrint ``2024/1234``, ``doi:…``,
    ``arxiv:…`` and ``nist:…`` likewise; None for a label of another scheme (an OpenAlex
    ID, a URL, an upload), which names no paper the ledger can confirm a quote of."""
    found = _LABEL.match(label.strip())
    if found is None:
        return None
    scheme, value = ("eprint", found.group(1)) if found.group(1) else found.group(2, 3)
    named = normal(scheme, value)
    return None if named is None else (scheme, named)


def library_label(scheme: str, value: str) -> str | None:
    """The library's work label for ledger identifier ``value`` of ``scheme``, as the
    library writes it: the inverse of ``library_paper``."""
    named = normal(scheme, value)
    if named is None:
        return None
    return f"eprint/{named}" if scheme == "eprint" else f"{scheme}:{named}"


def paper_id(scheme: str, value: str) -> str | None:
    """One key for one paper, whichever server named it: ``eprint:2024/1234``,
    ``doi:10.1007/…``; None for a value that is not of ``scheme``."""
    named = normal(scheme, value)
    return None if named is None else f"{scheme}:{named}"
