# SPDX-License-Identifier: MIT
"""Secrets taken out of what MCP traffic inspection records (docs/POLICY-MIDDLEWARE.md,
"MCP traffic inspection").

Everything the inspection records, a call's arguments, an answer's excerpt and the text
around a finding, passes through ``Redactor`` before it is written. Each secret is
replaced by ``[redacted:<kind>]``:

- ``credential``: a credential value the middleware knows. It runs before OpenShell
  injects credentials and holds none of their values, so this list is empty in
  production; the parameter is there for a deployment that learns some;
- ``placeholder``: an OpenShell credential placeholder (``openshell:resolve:...``);
- ``field``: the value of an object key with a secret-like name (``is_secret_name``: a
  name ending in ``token``, ``secret``, ``password``, ``passwd``, ``api_key``, ``apikey``,
  ``authorization``, ``cookie``, ``private_key`` or ``credential``, in any case, at any
  depth), in parsed JSON and, for excerpts that are text, in ``"name": value``,
  ``\\"name\\": \\"value\\"`` (JSON inside a JSON string) and ``name=value`` forms;
- ``private-key``: a PEM private key block, to its end or the end of the text;
- ``authorization``: an ``Authorization: ...`` value written as text;
- ``bearer``: a ``Bearer ...`` token;
- ``aws-key``: an AWS access key ID (``AKIA``/``ASIA`` and 16 characters);
- ``google-key``: a Google API key (``AIza`` and 35 characters);
- ``github-token``: a GitHub token (``ghp_``, ``gho_``, ``ghu_``, ``ghs_``, ``ghr_``,
  ``github_pat_``);
- ``jwt``: a JSON Web Token's shape (three base64url parts, the first starting ``eyJ``);
- ``cloudflare-secret``: a Cloudflare Access client secret written as its header
  (``CF-Access-Client-Secret: <hex>``). The bare hex value has no shape of its own that a
  SHA-256 digest does not share, so it is caught only by its name.

Patterns take time linear in their input: token patterns start only where a run of their
characters starts, and repetitions are bounded.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, cast

from sandbox_science.contracts.redaction import is_secret_name

_NAME_WORDS: Final = (
    r"[A-Za-z0-9_.-]{0,96}(?:token|secret|password|passwd|api[_-]?key|apikey|authorization"
    r"|credentials?|private[_-]?key|cookie)"
)


def _mark(kind: str) -> str:
    return f"[redacted:{kind}]"


@dataclass(frozen=True, slots=True)
class _Shape:
    """A secret's shape: the pattern, its kind, the group that is the secret (0: all of
    it) and, for a field, the group that must be a secret-like name."""

    regex: re.Pattern[str]
    kind: str
    group: int | str = 0
    name: int | None = None
    opens: str | None = None
    """The continuation mode when a match can run to the end of the text unfinished (its
    ``close`` group empty): the secret goes on in the next piece."""


_SHAPES: Final[tuple[_Shape, ...]] = (
    _Shape(re.compile(r"(?i)openshell:resolve:[A-Za-z0-9_.:/=+-]{0,512}"), "placeholder"),
    _Shape(
        re.compile(
            r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----[^-]*(?P<close>-----END [A-Z0-9 ]{0,40}"
            r"PRIVATE KEY-----|)"
        ),
        "private-key",
        opens="pem",
    ),
    _Shape(
        re.compile(
            r"(?i)cf-access-client-secret[\"'\\]{0,2}\s{0,4}[:=]\s{0,4}[\"'\\]{0,2}"
            r"([0-9a-f]{16,256})"
        ),
        "cloudflare-secret",
        1,
    ),
    _Shape(
        re.compile(
            r"(?i)\bauthorization[\"'\\]{0,2}\s{0,4}[:=]\s{0,4}[\"'\\]{0,2}"
            r"((?:(?:bearer|basic|token)\s{1,4})?[^\s\"',;\\]{1,4096})"
        ),
        "authorization",
        1,
    ),
    _Shape(re.compile(r"(?i)\bbearer\s{1,4}([A-Za-z0-9._~+/=-]{8,4096})"), "bearer", 1),
    _Shape(re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"), "aws-key"),
    _Shape(re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}"), "google-key"),
    _Shape(
        re.compile(
            r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})"
        ),
        "github-token",
    ),
    _Shape(
        re.compile(
            r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{4,8192}\.[A-Za-z0-9_-]{4,8192}"
            r"\.[A-Za-z0-9_-]{0,8192}"
        ),
        "jwt",
    ),
    # "name": value with a string, number or boolean value, in JSON text.
    _Shape(
        re.compile(
            r"\"([^\"\\]{1,128})\"\s{0,4}:\s{0,4}"
            r"(?:\"(?P<s>(?:[^\"\\]|\\[\s\S]|\\\Z)*)(?P<close>\"|\Z)"
            r"|(?P<n>-?[0-9][0-9.eE+-]{0,64}|true|false))"
        ),
        "field",
        "value",
        1,
        opens="json",
    ),
    # \"name\": \"value\": JSON written inside a JSON string, as tools often answer.
    _Shape(
        re.compile(
            r"\\\"([^\"\\]{1,128})\\\"\s{0,4}:\s{0,4}\\\"((?:[^\"\\]|\\[^\"]|\\\Z)*)(?P<close>\\\"|\Z)"
        ),
        "field",
        2,
        1,
        opens="escaped",
    ),
    # name=value or name: value in other text (environment files, YAML, URLs).
    _Shape(
        re.compile(
            rf"(?i)(?<![A-Za-z0-9_.-])({_NAME_WORDS})\s{{0,4}}[=:]\s{{0,4}}"
            r"([^\s,;&\"'\\{}\[\]]{1,4096})"
        ),
        "field",
        2,
    ),
)

_FIELD: Final = _mark("field")
_RUN_CHARS: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=_.~-"
)
"""Token characters: a run of them cut at an edge may be part of a secret."""
_CONTINUED: Final[dict[str, tuple[re.Pattern[str], str]]] = {
    "json": (re.compile(r"(?:[^\"\\]|\\[\s\S]|\\\Z)*(?P<close>\"|\Z)"), "field"),
    "escaped": (re.compile(r"(?:[^\"\\]|\\[^\"]|\\\Z)*(?P<close>\\\"|\Z)"), "field"),
    "pem": (
        re.compile(r"[^-]*(?P<close>-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----|)"),
        "private-key",
    ),
}
"""How a secret left open at the end of one text goes on at the start of the next."""
_HINT: Final = re.compile(
    r"(?i)token|secret|passw|api.?key|authoriz|credential|private.?key|cookie"
)
"""A name a secret field could have: without one, no field shape can match."""

Span = tuple[int, int, str]
Open = tuple[str, int]
"""A secret that runs to the end of the text: its continuation mode and where it began."""


def _span(shape: _Shape, match: re.Match[str]) -> tuple[int, int] | None:
    if shape.name is not None and not is_secret_name(match.group(shape.name)):
        return None
    if shape.group == "value":
        group = "s" if match.group("s") is not None else "n"
        return match.span(group)
    return match.span(shape.group)


def _open(shape: _Shape, match: re.Match[str], end: int) -> bool:
    """Whether the match is a secret left unfinished at the end of the text."""
    if shape.opens is None or match.end() != end:
        return False
    if shape.group == "value" and match.group("s") is None:
        return False
    return not match.group("close")


def trim_edges(text: str, *, head: bool, tail: bool) -> str:
    """``text`` without its leading (``head``) and trailing (``tail``) run of token
    characters: a plain scan, linear in what it drops."""
    start, end = 0, len(text)
    if head:
        while start < end and text[start] in _RUN_CHARS:
            start += 1
    if tail:
        while end > start and text[end - 1] in _RUN_CHARS:
            end -= 1
    return text[start:end]


class Redactor:
    """Takes secrets out of text and JSON values before they are recorded."""

    def __init__(self, known: Sequence[str] = ()) -> None:
        """``known`` are credential values to take out wherever they appear (the
        middleware knows none: OpenShell injects them after it decides)."""
        values = sorted({value for value in known if len(value) >= 8}, key=len, reverse=True)
        self._known = re.compile("|".join(re.escape(value) for value in values)) if values else None

    def spans(self, text: str) -> list[Span]:
        """Where ``text`` shows a secret: sorted, merged spans with the kind of the first."""
        return self.scan(text)[0]

    def scan(
        self, text: str, opening: str | None = None, *, only_open: bool = False
    ) -> tuple[list[Span], Open | None]:
        """The secret spans of ``text``, and the secret it leaves open at its end, if
        any. ``opening`` is the mode of a secret an earlier text left open, which goes on
        at the start of this one. With ``only_open``, only the shapes that can stay open
        are looked for (fields' values and private keys), to carry the state cheaply.
        Every pattern is linear in ``text``."""
        found: list[Span] = []
        left: Open | None = None
        if opening is not None:
            pattern, kind = _CONTINUED[opening]
            match = pattern.match(text)
            if match is not None:
                found.append((0, match.end(), kind))
                if match.end() == len(text) and not match.group("close"):
                    left = (opening, 0)
        if self._known is not None and not only_open:
            found += [(m.start(), m.end(), "credential") for m in self._known.finditer(text)]
        hinted = _HINT.search(text) is not None
        for shape in _SHAPES:
            if only_open and shape.opens is None:
                continue
            if shape.kind == "field" and not hinted:
                continue
            if shape.opens == "pem" and "-----BEGIN" not in text:
                continue
            for match in shape.regex.finditer(text):
                span = _span(shape, match)
                if span is None or (span[1] <= span[0] and not _open(shape, match, len(text))):
                    continue
                found.append((span[0], span[1], shape.kind))
                if shape.opens is not None and _open(shape, match, len(text)):
                    left = (shape.opens, span[0])
        found.sort(key=lambda span: (span[0], -span[1]))
        merged: list[Span] = []
        for start, end, kind in found:
            if merged and start < merged[-1][1]:
                last = merged[-1]
                merged[-1] = (last[0], max(last[1], end), last[2])
            elif end > start:
                merged.append((start, end, kind))
        return merged, left

    def text(self, text: str) -> str:
        """``text`` with every secret it shows replaced by ``[redacted:<kind>]``."""
        return _replace(text, self.spans(text), 0, len(text))

    def window(
        self,
        text: str,
        start: int,
        end: int,
        margin: int = 256,
        spans: list[Span] | None = None,
    ) -> str:
        """``text[start:end]`` redacted as part of the text around it: the secrets of all
        ``text`` (``spans``, when the caller has found them once for the whole text) or
        else of ``margin`` characters either side, so that a secret the window cuts, or
        whose name lies outside it, is taken out whole; and an unbroken run of token
        characters cut at either edge is dropped. The end of ``text`` counts as a cut,
        since more may follow."""
        if spans is not None:
            shown = _replace(text, spans, start, end)
        else:
            low, high = max(0, start - margin), min(len(text), end + margin)
            wide = text[low:high]
            shown = _replace(wide, self.spans(wide), start - low, end - low)
        head = 0 < start < len(text) and text[start - 1] in _RUN_CHARS and text[start] in _RUN_CHARS
        tail = end >= len(text) or (
            end > 0 and text[end] in _RUN_CHARS and text[end - 1] in _RUN_CHARS
        )
        return trim_edges(shown, head=head, tail=tail)

    def value(self, value: object) -> object:
        """A parsed JSON value with secret-named fields' values replaced, at any depth,
        and every string redacted as text."""
        if isinstance(value, dict):
            fields = cast("dict[str, object]", value)
            return {
                key: _FIELD if is_secret_name(key) else self.value(item)
                for key, item in fields.items()
            }
        if isinstance(value, list):
            return [self.value(item) for item in cast("list[object]", value)]
        if isinstance(value, str):
            return self.text(value)
        return value

    def json_text(self, encoded: bytes) -> str:
        """Encoded JSON as redacted compact text; text that is not JSON, or too deep to
        walk, is redacted as text."""
        text = encoded.decode("utf-8", "replace")
        try:
            walked = self.value(json.loads(text))
            return json.dumps(walked, ensure_ascii=False, separators=(",", ":"))
        except (ValueError, RecursionError):
            return self.text(text)


def _replace(text: str, spans: list[Span], start: int, end: int) -> str:
    """``text[start:end]`` with each secret span overlapping it replaced, whole, by its
    marker."""
    out: list[str] = []
    at = start
    for low, high, kind in spans:
        if high <= start or low >= end:
            continue
        out.append(text[at : max(low, at)])
        out.append(_mark(kind))
        at = max(at, high)
    if at < end:
        out.append(text[at:end])
    return "".join(out)


def cut(text: str, limit: int) -> tuple[str, bool]:
    """``text`` within ``limit`` UTF-8 bytes, never splitting a character, and whether it
    was cut. A cut text loses its last unbroken run of token characters, so that a secret
    the cut split leaves no prefix behind that the patterns could no longer recognize."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    kept = data[:limit].decode("utf-8", "ignore")
    return trim_edges(kept, head=False, tail=True), True
