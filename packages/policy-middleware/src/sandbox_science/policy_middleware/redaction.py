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


_SHAPES: Final[tuple[_Shape, ...]] = (
    _Shape(re.compile(r"(?i)openshell:resolve:[A-Za-z0-9_.:/=+-]{0,512}"), "placeholder"),
    _Shape(
        re.compile(
            r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----[^-]*(?:-----END [A-Z0-9 ]{0,40}"
            r"PRIVATE KEY-----)?"
        ),
        "private-key",
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
            r"(?:\"(?P<s>(?:[^\"\\]|\\.)*)\"|(?P<n>-?[0-9][0-9.eE+-]{0,64}|true|false))"
        ),
        "field",
        "value",
        1,
    ),
    # \"name\": \"value\": JSON written inside a JSON string, as tools often answer.
    _Shape(
        re.compile(r"\\\"([^\"\\]{1,128})\\\"\s{0,4}:\s{0,4}\\\"((?:[^\"\\]|\\[^\"])*)\\\""),
        "field",
        2,
        1,
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
_RUN_CLASS: Final = "A-Za-z0-9+/=_.~-"
_TOKEN_TAIL: Final = re.compile(rf"[{_RUN_CLASS}]{{1,8192}}$")
_TOKEN_HEAD: Final = re.compile(rf"^[{_RUN_CLASS}]{{1,8192}}")
_TOKEN: Final = re.compile(rf"[{_RUN_CLASS}]")

Span = tuple[int, int, str]


def _span(shape: _Shape, match: re.Match[str]) -> tuple[int, int] | None:
    if shape.name is not None and not is_secret_name(match.group(shape.name)):
        return None
    if shape.group == "value":
        group = "s" if match.group("s") is not None else "n"
        return match.span(group)
    return match.span(shape.group)


class Redactor:
    """Takes secrets out of text and JSON values before they are recorded."""

    def __init__(self, known: Sequence[str] = ()) -> None:
        """``known`` are credential values to take out wherever they appear (the
        middleware knows none: OpenShell injects them after it decides)."""
        values = sorted({value for value in known if len(value) >= 8}, key=len, reverse=True)
        self._known = re.compile("|".join(re.escape(value) for value in values)) if values else None

    def spans(self, text: str) -> list[Span]:
        """Where ``text`` shows a secret: sorted, merged spans with the kind of the first."""
        found: list[Span] = []
        if self._known is not None:
            found += [(m.start(), m.end(), "credential") for m in self._known.finditer(text)]
        for shape in _SHAPES:
            for match in shape.regex.finditer(text):
                span = _span(shape, match)
                if span is not None and span[1] > span[0]:
                    found.append((span[0], span[1], shape.kind))
        found.sort(key=lambda span: (span[0], -span[1]))
        merged: list[Span] = []
        for start, end, kind in found:
            if merged and start < merged[-1][1]:
                last = merged[-1]
                merged[-1] = (last[0], max(last[1], end), last[2])
            else:
                merged.append((start, end, kind))
        return merged

    def text(self, text: str) -> str:
        """``text`` with every secret it shows replaced by ``[redacted:<kind>]``."""
        return _replace(text, self.spans(text), 0, len(text))

    def window(self, text: str, start: int, end: int, margin: int = 256) -> str:
        """``text[start:end]`` redacted as part of the text around it: the secrets of
        ``margin`` characters either side are found first, so that a secret the window
        cuts, or whose name lies outside it, is still taken out whole, and an unbroken run
        of token characters cut at either edge is dropped. The end of ``text`` counts as
        a cut, since more may follow."""
        low, high = max(0, start - margin), min(len(text), end + margin)
        wide = text[low:high]
        shown = _replace(wide, self.spans(wide), start - low, end - low)
        if (
            0 < start < len(text)
            and _TOKEN.fullmatch(text[start - 1])
            and _TOKEN.fullmatch(text[start])
        ):
            shown = _TOKEN_HEAD.sub("", shown)
        if end >= len(text) or (
            end > 0 and _TOKEN.fullmatch(text[end]) and _TOKEN.fullmatch(text[end - 1])
        ):
            shown = _TOKEN_TAIL.sub("", shown)
        return shown

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
    return _TOKEN_TAIL.sub("", kept), True
