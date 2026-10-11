# SPDX-License-Identifier: MIT
"""A document's YAML front matter: its title and its macros (LD-4, LD-28).

```yaml
---
title: Fermat's little theorem
macros:
  \\F: \\mathbb{F}
---
```

The YAML is read with PyYAML's safe loader, without aliases (which can make a few bytes
expand into a great many), within a size limit. What it cannot read, or reads as
something other than a title and macros, is reported as a warning on the revision; the
text is kept as it is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final, cast

import yaml

MAX_FRONT_MATTER: Final = 16 * 1024
"""Characters of front matter read; longer front matter is not read."""
MAX_MACROS: Final = 200
MAX_MACRO: Final = 1000
"""Characters in one macro's definition."""

_MACRO_NAME: Final = re.compile(r"\\?([A-Za-z]+|[^A-Za-z\s])")


class _NoAliases(yaml.SafeLoader):
    """The safe loader, refusing aliases."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("aliases are not allowed in front matter")
        return super().compose_node(parent, index)  # pyright: ignore[reportUnknownMemberType]


@dataclass(frozen=True)
class FrontMatter:
    """What the front matter declares, and what in it could not be read."""

    title: str | None = None
    macros: dict[str, str] = field(default_factory=dict[str, str])
    problems: tuple[str, ...] = ()


def read_front_matter(content: str) -> FrontMatter:
    """The front matter between its ``---`` lines, as YAML."""
    if len(content) > MAX_FRONT_MATTER:
        return FrontMatter(
            problems=(f"front matter over {MAX_FRONT_MATTER} characters is not read",)
        )
    try:
        data: object = yaml.load(content, Loader=_NoAliases)  # noqa: S506 - a safe loader
    except (yaml.YAMLError, RecursionError, ValueError) as exc:
        return FrontMatter(problems=(f"the front matter is not YAML: {str(exc)[:200]}",))
    if data is None:
        return FrontMatter()
    if not isinstance(data, dict):
        return FrontMatter(problems=("the front matter is not a mapping",))
    fields = cast(dict[object, object], data)
    problems: list[str] = []
    title = fields.get("title")
    if title is not None and not isinstance(title, str | int | float):
        problems.append("title is not text")
        title = None
    macros: dict[str, str] = {}
    raw = fields.get("macros")
    if raw is not None:
        if not isinstance(raw, dict):
            problems.append("macros is not a mapping of names to definitions")
        else:
            for name, definition in cast(dict[object, object], raw).items():
                if len(macros) >= MAX_MACROS:
                    problems.append(f"only the first {MAX_MACROS} macros are used")
                    break
                key = str(name)
                match = _MACRO_NAME.fullmatch(key)
                if match is None or not isinstance(definition, str | int | float):
                    problems.append(f"macro {key[:40]!r} is not a name and a definition")
                    continue
                text = str(definition)
                if len(text) > MAX_MACRO:
                    problems.append(f"macro {key[:40]!r} is longer than {MAX_MACRO} characters")
                    continue
                macros["\\" + match[1]] = text
    return FrontMatter(
        title=None if title is None else str(title), macros=macros, problems=tuple(problems)
    )
