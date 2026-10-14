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

Macros are passed to KaTeX, which expands them as it renders, and a few short ones that
use one another can expand into a great many characters. So a macro is left out, with a
warning, when its definition defines another (as ``\\def`` does), uses one of its
arguments more than once (which would let it multiply what it is given), expands into
itself, or expands to more than ``MAX_EXPANDED`` characters with the macros it uses
expanded too. The browser checks the same as it renders, and bounds what each formula's
macros expand to in all (web/workspace/src/lib/macros.ts).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Final, cast

import yaml

MAX_FRONT_MATTER: Final = 16 * 1024
"""Characters of front matter read; longer front matter is not read."""
MAX_MACROS: Final = 200
MAX_MACRO: Final = 1000
"""Characters in one macro's definition."""
MAX_EXPANDED: Final = 2000
"""Characters a macro may expand to, the macros it uses expanded too."""

_MACRO_NAME: Final = re.compile(r"\\?([A-Za-z]+|[^A-Za-z\s])")
_CONTROL: Final = re.compile(r"\\(?:[A-Za-z]+|.)", re.DOTALL)
_ARGUMENT: Final = re.compile(r"#([1-9])")
_DEFINITIONS: Final = frozenset(
    {
        "\\def",
        "\\gdef",
        "\\edef",
        "\\xdef",
        "\\let",
        "\\futurelet",
        "\\global",
        "\\long",
        "\\outer",
        "\\newcommand",
        "\\renewcommand",
        "\\providecommand",
        "\\DeclareRobustCommand",
        "\\DeclareMathOperator",
        "\\newenvironment",
        "\\renewenvironment",
    }
)
"""Commands that define macros, as the browser refuses them in a formula."""


class _NoAliases(yaml.SafeLoader):
    """The safe loader, refusing aliases."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("aliases are not allowed in front matter")
        return super().compose_node(parent, index)


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
        title=None if title is None else str(title),
        macros=bounded_macros(macros, problems),
        problems=tuple(problems),
    )


def bounded_macros(macros: dict[str, str], problems: list[str]) -> dict[str, str]:
    """The macros without those that define others or expand without bound."""
    kept: dict[str, str] = {}
    for name, body in macros.items():
        defines = next(
            (found[0] for found in _CONTROL.finditer(body) if found[0] in _DEFINITIONS), None
        )
        if defines is not None:
            problems.append(f"macro {name} defines another ({defines}): it is left out")
            continue
        arguments = [found[1] for found in _ARGUMENT.finditer(body.replace("##", ""))]
        repeated = next((number for number in arguments if arguments.count(number) > 1), None)
        if repeated is not None:
            problems.append(f"macro {name} uses #{repeated} more than once: it is left out")
            continue
        kept[name] = body
    sizes: dict[str, float] = {}

    def expanded(name: str, visiting: set[str]) -> float:
        """What ``name`` expands to, in characters; infinite when it expands into itself."""
        if name in sizes:
            return sizes[name]
        if name in visiting:
            return math.inf
        visiting.add(name)
        body = kept[name]
        total: float = len(body)
        for found in _CONTROL.finditer(body):
            if found[0] in kept:
                total += expanded(found[0], visiting) - len(found[0])
                if total == math.inf:
                    break
        visiting.discard(name)
        sizes[name] = total
        return total

    bounded: dict[str, str] = {}
    for name, body in kept.items():
        size = expanded(name, set())
        if size == math.inf:
            problems.append(f"macro {name} expands into itself: it is left out")
        elif size > MAX_EXPANDED:
            problems.append(
                f"macro {name} expands to more than {MAX_EXPANDED} characters: it is left out"
            )
        else:
            bounded[name] = body
    return bounded
