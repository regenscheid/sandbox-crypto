# SPDX-License-Identifier: MIT
"""One paper under its identifiers (``papers``): the library's work labels map onto the
ledger's schemes and back, in one normal form, so that a reference and a library read
of its paper agree whichever way each was written."""

from __future__ import annotations

import pytest

from sandbox_science.orchestrator.papers import (
    exact_paper,
    library_label,
    library_paper,
    normal,
    paper_id,
)

TABLE = [
    # (library work label, ledger scheme, as a reference may record it, normal form)
    ("eprint/2024/1234", "eprint", "2024/1234", "2024/1234"),
    ("eprint/2024/059", "eprint", "2024/59", "2024/059"),
    ("eprint/2015/625", "eprint", "https://eprint.iacr.org/2015/625", "2015/625"),
    ("doi:10.1007/3-540-48910-x_15", "doi", "10.1007/3-540-48910-X_15", "10.1007/3-540-48910-x_15"),
    ("doi:10.1145/3133956.3134043", "doi", "https://doi.org/10.1145/3133956.3134043",
     "10.1145/3133956.3134043"),
    ("arxiv:2401.01234", "arxiv", "arXiv:2401.01234v2", "2401.01234"),
    ("arxiv:math.ag/0601001", "arxiv", "math.AG/0601001", "math.ag/0601001"),
    ("nist:ir-8610", "nist", "NIST IR 8610", "IR8610"),
    ("nist:fips.203", "nist", "FIPS 203", "FIPS203"),
]  # fmt: skip


@pytest.mark.parametrize(("label", "scheme", "recorded", "form"), TABLE)
def test_a_label_and_a_reference_name_one_paper(
    label: str, scheme: str, recorded: str, form: str
) -> None:
    assert library_paper(label) == (scheme, form)
    assert normal(scheme, recorded) == form
    assert paper_id(scheme, recorded) == f"{scheme}:{form}"
    # And back: the label the library writes for what a reference records.
    back = library_label(scheme, recorded)
    assert back is not None
    assert library_paper(back) == (scheme, form)


@pytest.mark.parametrize(
    "label",
    ["openalex:W1628272610", "url:https://example.test/p.pdf", "zbmath:1234.56789",
     "dblp:conf/x/Y24", "upload:" + "a" * 64, "2024/1234", "eprint/x", ""],
)  # fmt: skip
def test_a_label_of_another_scheme_names_no_ledger_paper(label: str) -> None:
    assert library_paper(label) is None


def test_a_value_not_of_its_scheme_has_no_normal_form() -> None:
    assert normal("eprint", "10.1007/x") is None
    assert normal("doi", "2024/1234") is None
    assert normal("arxiv", "not an id") is None
    assert normal("url", "https://example.test") is None
    assert normal("isbn", "978-3-16-148410-0") is None


@pytest.mark.parametrize(
    ("given", "named"),
    [("2017/634", ("eprint", "2017/634")), ("eprint/2017/634", ("eprint", "2017/634")),
     ("https://eprint.iacr.org/2017/634.pdf", ("eprint", "2017/634")),
     (" 10.1109/EuroSP.2018.00032 ", ("doi", "10.1109/eurosp.2018.00032")),
     ("doi:10.1109/eurosp.2018.00032", ("doi", "10.1109/eurosp.2018.00032")),
     ("https://doi.org/10.1109/eurosp.2018.00032", ("doi", "10.1109/eurosp.2018.00032")),
     ("arXiv:2401.01234v2", ("arxiv", "2401.01234")), ("nist:fips.203", ("nist", "FIPS203"))],
)  # fmt: skip
def test_an_exact_identifier_names_its_paper(given: str, named: tuple[str, str]) -> None:
    assert exact_paper(given) == named


@pytest.mark.parametrize(
    "given",
    ["CRYSTALS-Kyber", "Kyber: a CCA-secure module-lattice-based KEM", "ePrint 2017/634",
     "https://ieeexplore.ieee.org/document/10.1109/eurosp.2018.00032", "FIPS 203",
     "openalex:W1628272610", "W1628272610", "2017/634 and 2018/1", ""],
)  # fmt: skip
def test_anything_looser_than_one_exact_identifier_names_no_paper(given: str) -> None:
    """The library reads some of these too, by its lenient grammar or its catalogue's own
    names, but none is an identifier in a ledger scheme as it stands."""
    assert exact_paper(given) is None
