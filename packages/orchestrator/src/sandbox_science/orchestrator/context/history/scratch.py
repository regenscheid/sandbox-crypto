# SPDX-License-Identifier: MIT
"""Disposable JSON work tables for bounded historical transformations."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from typing import cast, overload

from .collections import Slice


class Scratch[T](Sequence[T]):
    """Process-generated data only; close the temporary file after the transformation."""

    def __init__(self) -> None:
        self.db = sqlite3.connect("")
        self.db.execute("PRAGMA cache_size=-512")
        self.db.execute(
            "CREATE TABLE items (pos INTEGER PRIMARY KEY, item TEXT, identity TEXT UNIQUE)"
        )
        self.length = 0

    def append(self, value: T, *, identity: str | None = None) -> bool:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
        written = self.db.execute(
            "INSERT OR IGNORE INTO items VALUES (?,?,?)", (self.length, text, identity)
        ).rowcount
        self.length += written
        return bool(written)

    def __len__(self) -> int:
        return self.length

    @overload
    def __getitem__(self, index: int) -> T: ...
    @overload
    def __getitem__(self, index: slice) -> Sequence[T]: ...
    def __getitem__(self, index: int | slice) -> T | Sequence[T]:
        if isinstance(index, slice):
            return Slice(self, range(*index.indices(self.length)))
        position = index if index >= 0 else self.length + index
        if not 0 <= position < self.length:
            raise IndexError(index)
        (text,) = self.db.execute("SELECT item FROM items WHERE pos=?", (position,)).fetchone()
        return cast(T, json.loads(text))

    def __iter__(self) -> Iterator[T]:
        for (text,) in self.db.execute("SELECT item FROM items ORDER BY pos"):
            yield cast(T, json.loads(text))

    def close(self) -> None:
        self.db.close()
