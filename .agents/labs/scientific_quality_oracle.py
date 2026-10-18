# SPDX-License-Identifier: MIT
"""Independent, offline oracle for the proposed inverse-S-box quality lab.

This is operator-side evaluation code, never an agent tool or staged answer key.
It uses integer polynomial arithmetic, independently of the researcher's Sage
implementation. ``selftest`` makes no network requests or model calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    """A binary polynomial basis, including its degree-n modulus bit."""

    degree: int
    modulus: int

    @property
    def order(self) -> int:
        return 1 << self.degree

    def multiply(self, left: int, right: int) -> int:
        if not 0 <= left < self.order or not 0 <= right < self.order:
            raise ValueError("operand outside this field")
        result = 0
        while right:
            if right & 1:
                result ^= left
            right >>= 1
            left <<= 1
            if left & self.order:
                left ^= self.modulus
        return result

    def power(self, value: int, exponent: int) -> int:
        if exponent < 0:
            raise ValueError("use the nonnegative finite-field inverse exponent")
        result = 1
        while exponent:
            if exponent & 1:
                result = self.multiply(result, value)
            value = self.multiply(value, value)
            exponent >>= 1
        return result

    def inverse_table(self) -> tuple[int, ...]:
        return (0, *(self.power(x, self.order - 2) for x in range(1, self.order)))


FIELDS = (Field(4, 0x13), Field(5, 0x25))


def ddt(table: tuple[int, ...]) -> tuple[tuple[int, ...], ...]:
    """Count ordered inputs for every XOR input/output difference."""
    size = len(table)
    if not size or size & (size - 1) or any(not 0 <= x < size for x in table):
        raise ValueError("expected a power-of-two truth table with n-bit outputs")
    rows = []
    for difference in range(size):
        row = [0] * size
        for value in range(size):
            row[table[value] ^ table[value ^ difference]] += 1
        rows.append(tuple(row))
    return tuple(rows)


def ddt_bytes(rows: tuple[tuple[int, ...], ...]) -> bytes:
    """The experiment's exact format: row-major unsigned 16-bit little-endian."""
    return b"".join(count.to_bytes(2, "little") for row in rows for count in row)


def nonlinearity(table: tuple[int, ...]) -> int:
    """Minimum component Hamming distance to all affine Boolean functions."""
    size = len(table)
    largest = max(
        abs(
            sum(
                1
                if ((input_mask & x).bit_count() ^ (output_mask & table[x]).bit_count()) % 2 == 0
                else -1
                for x in range(size)
            )
        )
        for input_mask in range(size)
        for output_mask in range(1, size)
    )
    return (size - largest) // 2


def measurements(field: Field) -> dict[str, object]:
    table = field.inverse_table()
    rows = ddt(table)
    uniformity = max(max(row) for row in rows[1:])
    witness = next(
        (a, b)
        for a in range(1, field.order)
        for b in range(field.order)
        if rows[a][b] == uniformity
    )
    a, b = witness
    cubic = tuple(field.power(x, 3) for x in range(field.order))
    return {
        "degree": field.degree,
        "modulus_hex": hex(field.modulus),
        "inverse_table": list(table),
        "truth_table_sha256": hashlib.sha256(bytes(table)).hexdigest(),
        "ddt_u16le_sha256": hashlib.sha256(ddt_bytes(rows)).hexdigest(),
        "differential_uniformity": uniformity,
        "max_differential_probability": f"{uniformity}/{field.order}",
        "witness": {
            "input_difference": a,
            "output_difference": b,
            "solutions": [x for x in range(field.order) if table[x] ^ table[x ^ a] == b],
        },
        "nonlinearity": nonlinearity(table),
        "ddt_nonzero_row_histogram": dict(
            sorted(Counter(x for row in rows[1:] for x in row).items())
        ),
        "cubic_is_permutation": len(set(cubic)) == field.order,
        "cubic_differential_uniformity": max(max(row) for row in ddt(cubic)[1:]),
    }


def selftest() -> dict[str, object]:
    """Cross-check the oracle using independent exhaustive inverse searches."""
    cases = []
    for field, expected in zip(FIELDS, ((4, 4, False), (2, 10, True)), strict=True):
        size = field.order
        inverse = field.inverse_table()
        for value in range(1, size):
            found = [other for other in range(1, size) if field.multiply(value, other) == 1]
            if found != [inverse[value]]:
                raise AssertionError("inverse exponent disagrees with exhaustive multiplication")
        rows = ddt(inverse)
        if rows[0] != (size,) + (0,) * (size - 1):
            raise AssertionError("wrong zero-difference row")
        if any(sum(row) != size for row in rows) or any(x % 2 for row in rows[1:] for x in row):
            raise AssertionError("invalid DDT row sum or characteristic-two pairing")
        # Rotate output coordinates and translate: a bijective affine output map.
        affine = tuple(((x << 1) & (size - 1) | (x >> (field.degree - 1))) ^ 3 for x in inverse)
        if Counter(x for row in ddt(affine)[1:] for x in row) != Counter(
            x for row in rows[1:] for x in row
        ):
            raise AssertionError("invertible affine output map changed the DDT spectrum")
        result = measurements(field)
        actual = (
            result["differential_uniformity"],
            result["nonlinearity"],
            result["cubic_is_permutation"],
        )
        if actual != expected:
            raise AssertionError((actual, expected))
        cases.append(result)
    return {"offline_selftest": "passed", "cases": cases}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("selftest", "answers"))
    args = parser.parse_args()
    output = (
        selftest() if args.action == "selftest" else {"cases": [measurements(f) for f in FIELDS]}
    )
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
