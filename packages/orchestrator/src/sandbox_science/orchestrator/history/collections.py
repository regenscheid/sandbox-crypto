# SPDX-License-Identifier: MIT
"""Scoped disk-backed run collections; slicing never loads the historical prefix."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator, MutableSequence, MutableSet, Sequence
from typing import Any, cast, overload

from sandbox_science.orchestrator.control import ControlStore


class Slice[T](MutableSequence[T]):
    """An immutable index view, including reversed slices, with constant resident size."""

    def __init__(self, source: Sequence[T], indices: range) -> None:
        self.source, self.indices = source, indices

    def __len__(self) -> int:
        return len(self.indices)

    @overload
    def __getitem__(self, index: int) -> T: ...
    @overload
    def __getitem__(self, index: slice) -> MutableSequence[T]: ...
    def __getitem__(self, index: int | slice) -> T | MutableSequence[T]:
        if isinstance(index, slice):
            return Slice(self.source, self.indices[index])
        return self.source[self.indices[index]]

    @overload
    def __setitem__(self, index: int, value: T) -> None: ...
    @overload
    def __setitem__(self, index: slice, value: Iterable[T]) -> None: ...
    def __setitem__(self, index: int | slice, value: T | Iterable[T]) -> None:
        raise TypeError("history slices are read-only")

    def __delitem__(self, index: int | slice) -> None:
        raise TypeError("history slices are read-only")

    def insert(self, index: int, value: T) -> None:
        raise TypeError("history slices are read-only")


class Collection[T](MutableSequence[T]):
    """Append-oriented run metadata, paged one value at a time from the control store.

    Message entries can be archive references; their decoder reconstructs the exact
    AssistantMessage (including native provider data) only when a consumer reads it.
    """

    def __init__(
        self,
        control: ControlStore,
        scope: tuple[str, str, str],
        name: str,
        *,
        encode: Callable[[T], object] = lambda value: value,
        decode: Callable[[Any], T] | None = None,
    ) -> None:
        self.control, self.scope, self.name = control, scope, name
        self.current: tuple[int, T] | None = None
        self.encode = encode

        def identity(value: Any) -> T:
            return cast(T, value)

        self.decode: Callable[[Any], T] = decode if decode is not None else identity
        with control.read() as tx:
            self.length = tx.sql.execute(
                "SELECT count(*) FROM run_collections WHERE project=? AND session=? AND run=? "
                "AND collection=?",
                (*scope, name),
            ).fetchone()[0]

    def __len__(self) -> int:
        return self.length

    @overload
    def __getitem__(self, index: int) -> T: ...
    @overload
    def __getitem__(self, index: slice) -> MutableSequence[T]: ...
    def __getitem__(self, index: int | slice) -> T | MutableSequence[T]:
        if isinstance(index, slice):
            return Slice(self, range(*index.indices(self.length)))
        position = self._index(index)
        if self.current is not None and self.current[0] == position:
            return self.current[1]
        with self.control.read() as tx:
            row = tx.sql.execute(
                "SELECT item FROM run_collections WHERE project=? AND session=? AND run=? "
                "AND collection=? AND position=?",
                (*self.scope, self.name, position),
            ).fetchone()
        if row is None:
            raise IndexError(index)
        return self.decode(json.loads(row[0]))

    def _index(self, index: int) -> int:
        position = index if index >= 0 else self.length + index
        if not 0 <= position < self.length:
            raise IndexError(index)
        return position

    def expose_current(self, index: int, value: T) -> None:
        """Keep only the current tool identity intact through live observers."""
        self.current = (self._index(index), value)

    def append_encoded(self, value: object) -> None:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
        with self.control.write() as tx:
            tx.sql.execute(
                "INSERT INTO run_collections VALUES (?,?,?,?,?,?)",
                (*self.scope, self.name, self.length, text),
            )
        self.length += 1

    def append(self, value: T) -> None:
        self.append_encoded(self.encode(value))

    def extend(self, values: Iterable[T]) -> None:
        for value in values:
            self.append(value)

    @overload
    def __setitem__(self, index: int, value: T) -> None: ...
    @overload
    def __setitem__(self, index: slice, value: Iterable[T]) -> None: ...
    def __setitem__(self, index: int | slice, value: T | Iterable[T]) -> None:
        if isinstance(index, slice):
            raise TypeError("run collections do not replace slices")
        if self.current is not None and self.current[0] == self._index(index):
            self.current = None
        text = json.dumps(self.encode(cast(T, value)), ensure_ascii=False, allow_nan=False)
        with self.control.write() as tx:
            tx.sql.execute(
                "UPDATE run_collections SET item=? WHERE project=? AND session=? AND run=? "
                "AND collection=? AND position=?",
                (text, *self.scope, self.name, self._index(index)),
            )

    def __delitem__(self, index: int | slice) -> None:
        raise TypeError("run evidence cannot be deleted")

    def insert(self, index: int, value: T) -> None:
        if index != self.length:
            raise TypeError("run collections only append")
        self.append(value)

    def __iter__(self) -> Iterator[T]:
        for index in range(self.length):
            yield self[index]


class Members(MutableSet[str]):
    """An indexed set with no resident identity cache."""

    def __init__(self, control: ControlStore, scope: tuple[str, str, str], name: str) -> None:
        self.control, self.scope, self.name = control, scope, name

    def __contains__(self, value: object) -> bool:
        if not isinstance(value, str):
            return False
        with self.control.read() as tx:
            return (
                tx.sql.execute(
                    "SELECT 1 FROM run_members WHERE project=? AND session=? AND run=? "
                    "AND collection=? AND value=?",
                    (*self.scope, self.name, value),
                ).fetchone()
                is not None
            )

    def __len__(self) -> int:
        with self.control.read() as tx:
            return tx.sql.execute(
                "SELECT count(*) FROM run_members WHERE project=? AND session=? AND run=? "
                "AND collection=?",
                (*self.scope, self.name),
            ).fetchone()[0]

    def __iter__(self) -> Iterator[str]:
        after = ""
        while True:
            with self.control.read() as tx:
                page = tx.sql.execute(
                    "SELECT value FROM run_members WHERE project=? AND session=? AND run=? "
                    "AND collection=? AND value>? ORDER BY value LIMIT 100",
                    (*self.scope, self.name, after),
                ).fetchall()
            if not page:
                return
            for (value,) in page:
                yield value
            after = page[-1][0]

    def add(self, value: str) -> None:
        with self.control.write() as tx:
            tx.sql.execute(
                "INSERT OR IGNORE INTO run_members VALUES (?,?,?,?,?)",
                (*self.scope, self.name, value),
            )

    def discard(self, value: str) -> None:
        with self.control.write() as tx:
            tx.sql.execute(
                "DELETE FROM run_members WHERE project=? AND session=? AND run=? "
                "AND collection=? AND value=?",
                (*self.scope, self.name, value),
            )

    def update(self, values: Iterable[str]) -> None:
        if isinstance(values, Members) and values.control is self.control:
            with self.control.write() as tx:
                tx.sql.execute(
                    "INSERT OR IGNORE INTO run_members SELECT ?,?,?,?,value FROM run_members "
                    "WHERE project=? AND session=? AND run=? AND collection=?",
                    (*self.scope, self.name, *values.scope, values.name),
                )
            return
        for value in values:
            self.add(value)
