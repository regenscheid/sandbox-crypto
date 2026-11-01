# SPDX-License-Identifier: MIT
"""Text with the secrets taken out, for what a service writes to its log.

Log lines are the one output of a trusted service that anything may reach (a journal, a
file, Loki), so a secret a message happens to carry must not survive into one. This is
a net for the shapes secrets take in free text, not a parser: URL user information,
``Authorization`` and ``Bearer`` values, ``name=value`` where the name ends in a secret
word, a token in a URL's query, and a few credential formats with a fixed prefix.

The worker's package-command redaction (``worker/provisioning.py``) is a stricter
relative for one job, command lines. It stays in that file because the sandbox's
executor loads the file alone, with the standard library only, so it cannot import this
module. Each pattern takes time linear in what it reads.
"""

from __future__ import annotations

import re
from typing import Final

REDACTED: Final = "***"
"""What a removed secret is replaced by."""

_USERINFO: Final = re.compile(r"(://)[^/?#\s]*@")
_AUTHORIZATION: Final = re.compile(
    r"(\bauthorization[\"']?\s*[=:]\s*)(?:(?:bearer|basic|token)\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;\"']+)",
    re.IGNORECASE,
)
_BEARER: Final = re.compile(r"\b(bearer)(\s+)[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_NAMED: Final = re.compile(
    r"([A-Za-z0-9_.-]*(?:token|secret|password|passwd|api[_-]?key|apikey|authorization"
    r"|credentials?|private[_-]?key|cookie))([\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)",
    re.IGNORECASE,
)
_QUERY: Final = re.compile(
    r"([?&;][A-Za-z0-9_.-]*(?:token|key|secret|password|passwd|auth|sig|signature)=)[^&;#\s]*",
    re.IGNORECASE,
)
_FORMATS: Final = re.compile(
    r"\bsk-[A-Za-z0-9_-]{16,}"
    r"|\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
)
_SECRET_NAME: Final = re.compile(
    r"(?:token|secret|password|passwd|api[_-]?key|apikey|authorization|credentials?"
    r"|private[_-]?key|cookie)$",
    re.IGNORECASE,
)


def is_secret_name(name: str) -> bool:
    """Whether a field named ``name`` holds a secret: it ends in ``token``, ``secret``,
    ``password``, ``api_key``, ``authorization``, ``credential``, ``private_key`` or
    ``cookie``. ``max_tokens`` and ``token_count`` do not."""
    return len(name) <= 128 and _SECRET_NAME.search(name) is not None


def redact_text(text: str) -> str:
    """``text`` with the secrets it holds replaced by ``***``."""
    text = _USERINFO.sub(rf"\1{REDACTED}@", text)
    text = _AUTHORIZATION.sub(rf"\1{REDACTED}", text)
    text = _BEARER.sub(rf"\1\2{REDACTED}", text)
    text = _NAMED.sub(rf"\1\2{REDACTED}", text)
    text = _QUERY.sub(rf"\1{REDACTED}", text)
    return _FORMATS.sub(REDACTED, text)
