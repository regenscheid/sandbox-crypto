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
from collections.abc import Callable, Sequence
from typing import Final, cast

from sandbox_science.contracts.redaction import is_secret_name

_NAME_WORDS: Final = (
    r"[A-Za-z0-9_.-]{0,96}(?:token|secret|password|passwd|api[_-]?key|apikey|authorization"
    r"|credentials?|private[_-]?key|cookie)"
)


def _mark(kind: str) -> str:
    return f"[redacted:{kind}]"


_PATTERNS: Final[tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...]] = (
    (re.compile(r"(?i)openshell:resolve:[A-Za-z0-9_.:/=+-]{0,512}"), _mark("placeholder")),
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----[^-]*(?:-----END [A-Z0-9 ]{0,40}"
            r"PRIVATE KEY-----)?"
        ),
        _mark("private-key"),
    ),
    (
        re.compile(
            r"(?i)(cf-access-client-secret)([\"'\\]{0,2}\s{0,4}[:=]\s{0,4}[\"'\\]{0,2})"
            r"[0-9a-f]{16,256}"
        ),
        rf"\1\2{_mark('cloudflare-secret')}",
    ),
    (
        re.compile(
            r"(?i)(\bauthorization[\"'\\]{0,2}\s{0,4}[:=]\s{0,4}[\"'\\]{0,2})"
            r"(?:(?:bearer|basic|token)\s{1,4})?[^\s\"',;\\]{1,4096}"
        ),
        rf"\1{_mark('authorization')}",
    ),
    (re.compile(r"(?i)\b(bearer)\s{1,4}[A-Za-z0-9._~+/=-]{8,4096}"), rf"\1 {_mark('bearer')}"),
    (re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"), _mark("aws-key")),
    (
        re.compile(
            r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})"
        ),
        _mark("github-token"),
    ),
    (
        re.compile(
            r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{4,8192}\.[A-Za-z0-9_-]{4,8192}"
            r"\.[A-Za-z0-9_-]{0,8192}"
        ),
        _mark("jwt"),
    ),
)
_JSON_FIELD: Final = re.compile(
    r"(\"([^\"\\]{1,128})\"\s{0,4}:\s{0,4})"
    r"(\"(?:[^\"\\]|\\.)*\"|-?[0-9][0-9.eE+-]{0,64}|true|false)"
)
"""``"name": value`` with a string, number or boolean value, in JSON text."""
_ESCAPED_FIELD: Final = re.compile(
    r"(\\\"([^\"\\]{1,128})\\\"\s{0,4}:\s{0,4})(\\\"(?:[^\"\\]|\\[^\"])*\\\")"
)
"""``\\"name\\": \\"value\\"``: JSON written inside a JSON string, as tools often answer."""
_NAMED: Final = re.compile(
    rf"(?i)(?<![A-Za-z0-9_.-])({_NAME_WORDS})(\s{{0,4}}[=:]\s{{0,4}})"
    r"([^\s,;&\"'\\{}\[\]]{1,4096})"
)
"""``name=value`` or ``name: value`` in other text (environment files, YAML, URLs)."""

_FIELD: Final = _mark("field")
_TOKEN_TAIL: Final = re.compile(r"[A-Za-z0-9+/=_.~-]{1,8192}$")


def _field(match: re.Match[str]) -> str:
    if not is_secret_name(match.group(2)):
        return match.group(0)
    escaped = match.group(3).startswith('\\"')
    return match.group(1) + (f'\\"{_FIELD}\\"' if escaped else f'"{_FIELD}"')


class Redactor:
    """Takes secrets out of text and JSON values before they are recorded."""

    def __init__(self, known: Sequence[str] = ()) -> None:
        """``known`` are credential values to take out wherever they appear (the
        middleware knows none: OpenShell injects them after it decides)."""
        values = sorted({value for value in known if len(value) >= 8}, key=len, reverse=True)
        self._known = re.compile("|".join(re.escape(value) for value in values)) if values else None

    def text(self, text: str) -> str:
        """``text`` with every secret it shows replaced by ``[redacted:<kind>]``."""
        if self._known is not None:
            text = self._known.sub(_mark("credential"), text)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        text = _JSON_FIELD.sub(_field, text)
        text = _ESCAPED_FIELD.sub(_field, text)
        return _NAMED.sub(rf"\1\2{_FIELD}", text)

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


def cut(text: str, limit: int) -> tuple[str, bool]:
    """``text`` within ``limit`` UTF-8 bytes, never splitting a character, and whether it
    was cut. A cut text loses its last unbroken run of token characters, so that a secret
    the cut split leaves no prefix behind that the patterns could no longer recognize."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    kept = data[:limit].decode("utf-8", "ignore")
    return _TOKEN_TAIL.sub("", kept), True
