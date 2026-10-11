# SPDX-License-Identifier: MIT
"""Check that a git upload-pack request body is well formed (docs/POLICY.md, "Residual risks").

The policy library classifies ``POST <repo>/git-upload-pack`` with git's request content
type as a git fetch, which policies often allow from many hosts. The POST still delivers a
request body to the host, so the middleware calls :func:`check_upload_pack_request` and
denies the request unless the body is an upload-pack request in the form git clients send:
pkt-lines carrying known keywords, object IDs and bounded values, and nothing after the
end of the request.

The grammar below follows git's protocol documents (gitprotocol-common, -pack, -http,
-capabilities and -v2), checked against the clients that write requests (git's
``fetch-pack.c``, ``connect.c``, ``remote-curl.c`` and ``fetch-object-info.c``; libgit2's
``smart_pkt.c``; dulwich's ``client.py``; go-git's ``packp``) and against request bodies
captured from git 2.53 (``tests/fixtures/git``). Where the documents and the clients
disagree, this module accepts what the clients send; those places are marked
"Real clients:".

Framing::

    body      = 1*pkt                          ; nothing may follow the last pkt
    pkt       = flush-pkt / delim-pkt / data-pkt
    flush-pkt = "0000"
    delim-pkt = "0001"                         ; protocol v2 only
    data-pkt  = pkt-len payload                ; pkt-len: 4 lowercase hex digits, 5 to 65520
    payload   = line [LF]                      ; no other control characters

Protocol v0 and v1 (no ``Git-Protocol`` header, or ``version=0`` or ``version=1``)::

    request    = want-list flush-pkt [*have (flush-pkt / "done")]
    want-list  = first-want *("want" SP oid)
                 *("shallow" SP oid / "deepen" SP depth / "deepen-since" SP time /
                   "deepen-not" SP ref / "filter" SP filter-spec)
    first-want = "want" SP oid [SP capability *(SP capability)] [SP]
    have       = "have" SP oid

Protocol v2 (``Git-Protocol: version=2``)::

    request    = *capability delim-pkt *argument flush-pkt
                 ; exactly one capability line is "command=" (ls-refs, fetch,
                 ; object-info or bundle-uri), in any position
    capability = "command=" name / "agent=" agent / "object-format=" ("sha1" / "sha256") /
                 "session-id=" id / "server-option=" text / "promisor-remote=" names

A body consisting of a single flush-pkt is accepted in either dialect: it is the v2 empty
request, and git sends it to probe authentication before a request larger than
``http.postBuffer`` (``probe_rpc`` in ``remote-curl.c``), without a ``Git-Protocol`` header.

The dialect comes from the ``Git-Protocol`` header, as git's server chooses it: absent,
``version=0`` or ``version=1`` selects v0, ``version=2`` selects v2, and any other value
is rejected. A body in the other dialect is rejected.

A gzip body (``Content-Encoding: gzip`` or ``x-gzip``, or starting with the gzip magic
bytes when the header is not visible) must be one gzip member with no optional header
fields, must decompress to at most ``max_decoded_bytes``, must have a matching trailer and
nothing after it, and must not be larger than an uncompressed encoding of its content
would be. git compresses request bodies over 1024 bytes that fit in ``http.postBuffer``
(``post_rpc`` in ``remote-curl.c``) into exactly such a member.

The check stops non-git payloads and makes any data git-shaped; it does not bound volume.
A well-formed request still carries data of the client's choosing: object IDs in ``want``,
``have``, ``shallow`` and ``oid`` lines are arbitrary to this check (it cannot see the
repository), about 40% of a body of ``have`` lines; ``ref-prefix``, ``want-ref``,
``deepen-not`` and ``server-option`` values are bounded in length but not in number; and
deflate encoding choices in a gzip body add up to about its size again.
"""

from __future__ import annotations

import re
import struct
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Final
from urllib.parse import unquote_to_bytes

__all__ = [
    "MAX_AGENT_BYTES",
    "MAX_DECODED_BYTES",
    "MAX_FILTER_BYTES",
    "MAX_PKT_LEN",
    "MAX_PROMISOR_REMOTE_BYTES",
    "MAX_REF_BYTES",
    "MAX_SERVER_OPTION_BYTES",
    "MAX_SESSION_ID_BYTES",
    "GitRequestError",
    "check_upload_pack_request",
]


class GitRequestError(ValueError):
    """The body is not a well-formed git upload-pack request."""


MAX_DECODED_BYTES: Final[int] = 4 * 1024 * 1024
"""Default cap on the request size after removing any gzip encoding (OpenShell's body limit)."""

MAX_PKT_LEN: Final[int] = 65520
"""Largest pkt-line git sends, length field included (LARGE_PACKET_MAX in pkt-line.h)."""

# Bounds on free-form values. git's own values are far shorter (an agent such as
# "git/2.53.0-Linux", a trace2 session ID of about 45 bytes, short ref names and filter
# specs); the bounds leave room for unusual but legitimate values and nothing more.
MAX_AGENT_BYTES: Final[int] = 256
MAX_SESSION_ID_BYTES: Final[int] = 1024
MAX_SERVER_OPTION_BYTES: Final[int] = 1024
MAX_PROMISOR_REMOTE_BYTES: Final[int] = 1024
MAX_REF_BYTES: Final[int] = 1024
MAX_FILTER_BYTES: Final[int] = 1024

_GZIP_MAGIC: Final = b"\x1f\x8b"
_GZIP_HEADER_LEN: Final = 10
_GZIP_TRAILER_LEN: Final = 8
_DEFLATE: Final = 8
# git's deflate output is about half the size of a request. Even a stored (uncompressed)
# deflate stream adds only 5 bytes per block plus the 18-byte gzip wrapper, so this bound
# never rejects a real encoder; it stops a body from smuggling extra data in deflate
# encoding choices (padding blocks, unused Huffman codes) beyond the size of the request.
_GZIP_SLACK: Final = 64

_SHA1_HEX: Final = 40
_SHA256_HEX: Final = 64
_OBJECT_FORMATS: Final[Mapping[bytes, int]] = {b"sha1": _SHA1_HEX, b"sha256": _SHA256_HEX}
_MAX_DEPTH: Final = 2**31 - 1  # git sends "deepen %d"; --unshallow sends INFINITE_DEPTH (2^31-1)
_MAX_TIMESTAMP: Final = 2**64 - 2  # timestamp_t is unsigned; upload-pack rejects -1 (2^64-1)
_MAX_FILTER_NESTING: Final = 4

_PKT_LEN_RE: Final = re.compile(rb"[0-9a-f]{4}")
# Every byte that is not printable ASCII, non-ASCII text or the LF ending a payload.
_CONTROL_RE: Final = re.compile(rb"[\x00-\x09\x0b-\x1f\x7f]")
_HEX_RE: Final = re.compile(rb"[0-9a-f]+")
_DEPTH_RE: Final = re.compile(rb"[1-9][0-9]{0,9}")
_TIMESTAMP_RE: Final = re.compile(rb"0|[1-9][0-9]{0,19}")
_AGENT_RE: Final = re.compile(rb"[\x21-\x7e]+")  # gitprotocol-v2: printable ASCII, no space
_SESSION_ID_RE: Final = re.compile(rb"[\x21-\x7e]+")  # no whitespace or non-printables
_SERVER_OPTION_RE: Final = re.compile(rb"[\x20-\x7e]*")  # git forbids only NUL and LF
# promisor-remote=<names>: URL-encoded remote names separated by ';' (promisor-remote.c
# leaves printable ASCII other than ',', ';' and '%' unencoded).
_PROMISOR_NAME: Final = rb"(?:[\x21-\x24\x26-\x2b\x2d-\x3a\x3c-\x7e]|%[0-9a-fA-F]{2})+"
_PROMISOR_REMOTE_RE: Final = re.compile(_PROMISOR_NAME + rb"(?:;" + _PROMISOR_NAME + rb")*")
# Ref names follow git check-ref-format: no control characters, space, '~', '^', ':', '?',
# '*', '[' or '\'. Real clients: ref names may be UTF-8 (git sends "ref-prefix
# refs/heads/fünf" for a branch of that name), so non-ASCII text is allowed here.
_REF_RE: Final = re.compile(r"[^\x00-\x20\x7f-\x9f~^:?*\[\\]+")
# packfile-uris lists the protocols the client accepts; git sends only http and https.
_URI_PROTOCOLS_RE: Final = re.compile(rb"https?(?:,https?)*")
_MAX_URI_PROTOCOLS_BYTES: Final = 64
# Filter specs (list-objects-filter-options.c). Sizes may carry a k, m or g suffix because
# git sends tree depths, and blob limits inside combine:, as the user typed them.
_FILTER_SIZE_RE: Final = re.compile(rb"[0-9]{1,20}[kKmMgG]?")
_FILTER_OBJECT_TYPES: Final = frozenset({b"blob", b"tree", b"commit", b"tag"})
_SPARSE_OID_RE: Final = re.compile(rb"[\x20-\x7e]+")
# A combine: sub-filter as git encodes it: unreserved characters and %XX escapes only.
_COMBINE_PART_RE: Final = re.compile(rb"(?:[-./0-9:=A-Z_a-z|]|%[0-9a-fA-F]{2})+")

# Capabilities a v0/v1 client may request on its first want line: those git's upload-pack
# advertises, minus server-only ones (symref, allow-*-sha1-in-want). Real clients: dulwich
# requests "shallow" and both multi_ack variants; libgit2 requests "shallow".
_V0_FLAG_CAPABILITIES: Final = frozenset(
    {
        b"multi_ack",
        b"multi_ack_detailed",
        b"no-done",
        b"thin-pack",
        b"side-band",
        b"side-band-64k",
        b"ofs-delta",
        b"shallow",
        b"deepen-since",
        b"deepen-not",
        b"deepen-relative",
        b"no-progress",
        b"include-tag",
        b"filter",
    }
)
_FETCH_FLAGS: Final = frozenset(
    {
        b"thin-pack",
        b"no-progress",
        b"include-tag",
        b"ofs-delta",
        b"sideband-all",
        b"deepen-relative",
        b"done",
        b"wait-for-done",
    }
)
_LS_REFS_FLAGS: Final = frozenset({b"peel", b"symrefs", b"unborn"})
_OBJECT_INFO_FLAGS: Final = frozenset({b"size", b"type"})


class _Control(Enum):
    """Special packets with a length below 4."""

    FLUSH = "flush-pkt"
    DELIM = "delim-pkt"


type _Packet = bytes | _Control


def check_upload_pack_request(
    body: bytes,
    *,
    content_encoding: str | None = None,
    git_protocol: str | None = None,
    max_decoded_bytes: int = MAX_DECODED_BYTES,
) -> None:
    """Raise GitRequestError unless ``body`` is a well-formed upload-pack request.

    ``body`` must be the complete request body as sent: a caller that only has a truncated
    body (for example one over its buffer limit) must deny the request instead of checking
    the prefix. ``content_encoding`` and ``git_protocol`` are the values of the
    ``Content-Encoding`` and ``Git-Protocol`` headers, or None when the header is absent.
    A caller that sees a header more than once must deny the request or pass the values
    joined with ", ", which is rejected. Gzip bodies are recognized by their magic bytes
    even when ``content_encoding`` is None, because the proxy may not show that header.
    ``max_decoded_bytes`` bounds the request size after decompression, and the plain body
    size when it is not compressed.
    """
    if max_decoded_bytes < 0:
        raise ValueError("max_decoded_bytes must not be negative")
    version = _protocol_version(git_protocol)
    data = _decode_body(body, content_encoding, max_decoded_bytes)
    if not data:
        raise GitRequestError("empty request body")
    packets = _split_packets(data)
    if packets == [_Control.FLUSH]:
        return
    if version == 2:
        _check_v2(packets)
    else:
        _check_v0(packets)


def _show(value: bytes, limit: int = 48) -> str:
    """Render untrusted bytes for an error message, escaped and truncated."""
    shown = repr(value[:limit])
    return shown + "..." if len(value) > limit else shown


def _protocol_version(git_protocol: str | None) -> int:
    """Map the Git-Protocol header to the request dialect, as git's server reads it."""
    if git_protocol is None:
        return 0
    value = git_protocol.strip(" \t")
    # git sends exactly "version=1" or "version=2" (get_protocol_http_header in
    # remote-curl.c); version 1 requests use the v0 grammar. Other values, several
    # parameters and repeated headers are not what git sends.
    if value in ("version=0", "version=1"):
        return 0
    if value == "version=2":
        return 2
    raise GitRequestError(f"unsupported Git-Protocol header {git_protocol[:64]!r}")


def _declares_gzip(content_encoding: str | None) -> bool:
    """Return whether Content-Encoding names gzip; reject every other content coding."""
    if content_encoding is None:
        return False
    # Content codings are case-insensitive (RFC 9110, section 8.4.1). git's server accepts
    # "gzip" and "x-gzip" (http-backend.c); git sends "gzip".
    if content_encoding.strip(" \t").lower() in ("gzip", "x-gzip"):
        return True
    raise GitRequestError(f"unsupported Content-Encoding {content_encoding[:64]!r}")


def _decode_body(body: bytes, content_encoding: str | None, limit: int) -> bytes:
    """Undo gzip encoding (declared or recognized by magic bytes) within the size limit."""
    declared_gzip = _declares_gzip(content_encoding)
    if body.startswith(_GZIP_MAGIC):
        # A pkt-line body starts with a hex digit, so the magic bytes are unambiguous.
        return _gunzip(body, limit)
    if declared_gzip:
        raise GitRequestError("Content-Encoding is gzip but the body is not gzip data")
    if len(body) > limit:
        raise GitRequestError(f"request body is larger than {limit} bytes")
    return body


def _gunzip(body: bytes, limit: int) -> bytes:
    """Decompress exactly one gzip member, producing at most ``limit`` bytes."""
    if len(body) < _GZIP_HEADER_LEN + _GZIP_TRAILER_LEN:
        raise GitRequestError("truncated gzip data")
    if body[2] != _DEFLATE:
        raise GitRequestError("gzip data must use the deflate method")
    if body[3] != 0:
        # zlib (git), Java and Go write no optional header fields. FEXTRA, FNAME and
        # FCOMMENT would carry arbitrary bytes that the git server skips.
        raise GitRequestError("gzip header flags are not allowed")
    # The fixed header's MTIME, XFL and OS bytes vary between zlib builds and clients and
    # are not checked; they are 6 bytes per request.
    inflater = zlib.decompressobj(wbits=-zlib.MAX_WBITS)
    try:
        # max_length stops inflation one byte past the limit, so a decompression bomb
        # costs at most ``limit`` bytes of output.
        data = inflater.decompress(body[_GZIP_HEADER_LEN:], limit + 1)
    except zlib.error as exc:
        raise GitRequestError(f"invalid deflate data in gzip body: {exc}") from exc
    if len(data) > limit:
        raise GitRequestError(f"gzip body decompresses to more than {limit} bytes")
    if not inflater.eof:
        raise GitRequestError("truncated deflate data in gzip body")
    trailer = inflater.unused_data
    if len(trailer) != _GZIP_TRAILER_LEN:
        # git writes a single member; a second member or any other trailing bytes would
        # reach the server without being inflated by it (http-backend stops at the end of
        # the first member).
        if len(trailer) > _GZIP_TRAILER_LEN:
            raise GitRequestError("data after the gzip member")
        raise GitRequestError("truncated gzip trailer")
    crc, size = struct.unpack("<II", trailer)
    if crc != zlib.crc32(data) or size != len(data) % 2**32:
        raise GitRequestError("gzip trailer does not match the decompressed data")
    if len(body) > len(data) + len(data) // 1024 + _GZIP_SLACK:
        raise GitRequestError("gzip body is larger than the request it encodes")
    return data


def _split_packets(data: bytes) -> list[_Packet]:
    """Split a body into pkt-lines, dropping one trailing LF from each payload."""
    control = _CONTROL_RE.search(data)
    if control is not None:
        raise GitRequestError(f"control character at byte {control.start()}")
    packets: list[_Packet] = []
    pos = 0
    size = len(data)
    while pos < size:
        header = data[pos : pos + 4]
        # Real clients: git's reader accepts upper-case hex in pkt-len, but git, libgit2,
        # dulwich, go-git and JGit all write lower case.
        if len(header) < 4 or _PKT_LEN_RE.fullmatch(header) is None:
            raise GitRequestError(f"bad pkt-line length {_show(header)} at byte {pos}")
        length = int(header, 16)
        if length == 0:
            packets.append(_Control.FLUSH)
        elif length == 1:
            packets.append(_Control.DELIM)
        elif length == 2:
            raise GitRequestError(f"response-end packet at byte {pos}; requests never contain one")
        elif length == 3:
            raise GitRequestError(f"invalid pkt-line length 0003 at byte {pos}")
        elif length == 4:
            # gitprotocol-common: implementations SHOULD NOT send an empty pkt-line, and
            # no client puts one in an upload-pack request.
            raise GitRequestError(f"empty pkt-line at byte {pos}")
        elif length > MAX_PKT_LEN:
            raise GitRequestError(f"pkt-line of {length} bytes at byte {pos} exceeds {MAX_PKT_LEN}")
        elif pos + length > size:
            raise GitRequestError(f"truncated pkt-line at byte {pos}")
        else:
            payload = data[pos + 4 : pos + length]
            if payload.endswith(b"\n"):
                payload = payload[:-1]
            if b"\n" in payload:
                raise GitRequestError(f"line feed inside the pkt-line at byte {pos}")
            packets.append(payload)
            pos += length
            continue
        pos += 4
    return packets


@dataclass(slots=True)
class _ObjectFormat:
    """Length of the hex object IDs in one request, once known."""

    hex_len: int | None = None

    def check(self, value: bytes, keyword: str) -> None:
        """Require a lower-case hex object ID of the request's length."""
        expected = self.hex_len
        if expected is None and len(value) in (_SHA1_HEX, _SHA256_HEX):
            # Protocol v0 does not name the object format; the server's repository fixes
            # it, so every object ID in a request has the length of the first.
            expected = len(value)
        if len(value) != expected or _HEX_RE.fullmatch(value) is None:
            raise GitRequestError(f"invalid object ID in {keyword} line: {_show(value)}")
        self.hex_len = expected


def _keyword(line: bytes) -> tuple[bytes, bytes | None]:
    """Split ``keyword SP value``; the value is None when the line has no space."""
    keyword, space, value = line.partition(b" ")
    return keyword, value if space else None


def _name(keyword: bytes) -> str:
    return keyword.decode("ascii", "backslashreplace")


def _once(seen: set[bytes], keyword: bytes) -> None:
    if keyword in seen:
        raise GitRequestError(f"{_name(keyword)!r} appears more than once")
    seen.add(keyword)


def _check_bounded(value: bytes, pattern: re.Pattern[bytes], limit: int, what: str) -> None:
    if len(value) > limit:
        raise GitRequestError(f"{what} is longer than {limit} bytes")
    if pattern.fullmatch(value) is None:
        raise GitRequestError(f"invalid {what}: {_show(value)}")


def _check_ref(value: bytes, what: str, *, allow_empty: bool = False) -> None:
    """Check a ref name or ref prefix: bounded, UTF-8, no characters refs may not contain."""
    if len(value) > MAX_REF_BYTES:
        raise GitRequestError(f"{what} is longer than {MAX_REF_BYTES} bytes")
    if not value and allow_empty:
        # Real clients: a fetch refspec glob at the start ("*:refs/x/*") makes git send an
        # empty ref-prefix.
        return
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        raise GitRequestError(f"{what} is not UTF-8: {_show(value)}") from None
    if _REF_RE.fullmatch(text) is None:
        raise GitRequestError(f"invalid {what}: {_show(value)}")


def _check_depth(value: bytes) -> None:
    if _DEPTH_RE.fullmatch(value) is None or int(value) > _MAX_DEPTH:
        raise GitRequestError(f"invalid deepen depth: {_show(value)}")


def _check_timestamp(value: bytes) -> None:
    if _TIMESTAMP_RE.fullmatch(value) is None or int(value) > _MAX_TIMESTAMP:
        raise GitRequestError(f"invalid deepen-since time: {_show(value)}")


def _check_filter(spec: bytes, nesting: int = 0) -> None:
    """Check a filter spec as git sends it (expand_list_objects_filter_spec)."""
    if not spec or len(spec) > MAX_FILTER_BYTES:
        raise GitRequestError(f"filter spec must be 1 to {MAX_FILTER_BYTES} bytes")
    if spec == b"blob:none":
        return
    for prefix, check in _FILTER_FORMS:
        if spec.startswith(prefix):
            check(spec[len(prefix) :], nesting)
            return
    # "auto" is resolved by the client before sending, and the server rejects it and the
    # removed sparse:path filter.
    raise GitRequestError(f"unknown filter spec: {_show(spec)}")


def _check_filter_size(value: bytes, nesting: int) -> None:
    if _FILTER_SIZE_RE.fullmatch(value) is None:
        raise GitRequestError(f"invalid filter size: {_show(value)}")


def _check_filter_object_type(value: bytes, nesting: int) -> None:
    if value not in _FILTER_OBJECT_TYPES:
        raise GitRequestError(f"invalid filter object type: {_show(value)}")


def _check_filter_sparse_oid(value: bytes, nesting: int) -> None:
    if _SPARSE_OID_RE.fullmatch(value) is None:
        raise GitRequestError(f"invalid sparse:oid filter value: {_show(value)}")


def _check_filter_combine(value: bytes, nesting: int) -> None:
    if nesting >= _MAX_FILTER_NESTING:
        raise GitRequestError("combine: filters are nested too deeply")
    for part in value.split(b"+"):
        # git encodes each sub-filter with %XX escapes for reserved characters, and the
        # server rejects unescaped ones; a sub-filter may itself be a combine: filter
        # when several --filter options are given.
        if _COMBINE_PART_RE.fullmatch(part) is None:
            raise GitRequestError(f"invalid combine: sub-filter: {_show(part)}")
        _check_filter(unquote_to_bytes(part), nesting + 1)


_FILTER_FORMS: Final[tuple[tuple[bytes, Callable[[bytes, int], None]], ...]] = (
    (b"blob:limit=", _check_filter_size),
    (b"tree:", _check_filter_size),
    (b"object:type=", _check_filter_object_type),
    (b"sparse:oid=", _check_filter_sparse_oid),
    (b"combine:", _check_filter_combine),
)


# --- protocol v0 and v1 ------------------------------------------------------------------


def _check_v0(packets: list[_Packet]) -> None:
    """Check a v0/v1 stateless request: wants, then optionally one round of haves."""
    first = packets[0]
    if not isinstance(first, bytes) or not first.startswith(b"want "):
        if _Control.DELIM in packets or (isinstance(first, bytes) and b"=" in first):
            raise GitRequestError(
                "body looks like a protocol v2 request, but the Git-Protocol header does "
                "not select version 2"
            )
        raise GitRequestError("a protocol v0/v1 request must start with a want line")
    oids = _ObjectFormat()
    _check_first_want(first, oids)
    index = 1
    while index < len(packets):
        packet = packets[index]
        if not isinstance(packet, bytes) or not packet.startswith(b"want "):
            break
        oids.check(packet[len(b"want ") :], "want")
        index += 1
    index = _check_v0_want_options(packets, index, oids)
    if index == len(packets):
        # git sends the want list on its own to learn the shallow boundary first.
        return
    while index < len(packets):
        packet = packets[index]
        if not isinstance(packet, bytes) or not packet.startswith(b"have "):
            break
        oids.check(packet[len(b"have ") :], "have")
        index += 1
    if index == len(packets):
        raise GitRequestError("have lines must end with a flush-pkt or done")
    end = packets[index]
    if end is not _Control.FLUSH and end != b"done":
        raise GitRequestError(f"expected have, done or flush-pkt, got {_describe(end)}")
    if index + 1 != len(packets):
        raise GitRequestError("data after the end of the request")


def _check_first_want(line: bytes, oids: _ObjectFormat) -> None:
    """Check ``want <oid> [capabilities]``, the only line that carries capabilities."""
    oid, _, capabilities = line[len(b"want ") :].partition(b" ")
    # Real clients: libgit2 and dulwich end the capability list with one space ("want
    # <oid> cap1 cap2 \n"), which leaves "want <oid> \n" when there are no capabilities.
    if capabilities:
        if capabilities.endswith(b" "):
            capabilities = capabilities[:-1]
        _check_v0_capabilities(capabilities.split(b" "), oids)
    oids.check(oid, "want")


def _check_v0_capabilities(tokens: list[bytes], oids: _ObjectFormat) -> None:
    seen: set[bytes] = set()
    for token in tokens:
        name, equals, value = token.partition(b"=")
        _once(seen, name)
        if not equals and name in _V0_FLAG_CAPABILITIES:
            continue
        if equals and name == b"agent":
            _check_bounded(value, _AGENT_RE, MAX_AGENT_BYTES, "agent")
        elif equals and name == b"session-id":
            _check_bounded(value, _SESSION_ID_RE, MAX_SESSION_ID_BYTES, "session-id")
        elif equals and name == b"object-format":
            oids.hex_len = _object_format(value)
        else:
            raise GitRequestError(f"unknown capability {_show(token)}")


def _check_v0_want_options(packets: list[_Packet], index: int, oids: _ObjectFormat) -> int:
    """Check the shallow, deepen and filter lines and the flush-pkt ending the want list."""
    seen: set[bytes] = set()
    while index < len(packets):
        packet = packets[index]
        if not isinstance(packet, bytes):
            break
        keyword, value = _keyword(packet)
        if value is None:
            raise GitRequestError(f"unexpected line {_show(packet)} in the want list")
        if keyword == b"shallow":
            oids.check(value, "shallow")
        elif keyword == b"deepen":
            _once(seen, keyword)
            _check_depth(value)
        elif keyword == b"deepen-since":
            _once(seen, keyword)
            _check_timestamp(value)
        elif keyword == b"deepen-not":
            _check_ref(value, "deepen-not ref")
        elif keyword == b"filter":
            _once(seen, keyword)
            _check_filter(value)
        elif keyword == b"want":
            raise GitRequestError("want lines must come before shallow, deepen and filter lines")
        else:
            raise GitRequestError(f"unexpected line {_show(packet)} in the want list")
        index += 1
    if index == len(packets) or packets[index] is not _Control.FLUSH:
        found = "the end of the body" if index == len(packets) else _describe(packets[index])
        raise GitRequestError(f"want list must end with a flush-pkt, got {found}")
    return index + 1


def _describe(packet: _Packet) -> str:
    return packet.value if isinstance(packet, _Control) else _show(packet)


def _object_format(value: bytes) -> int:
    hex_len = _OBJECT_FORMATS.get(value)
    if hex_len is None:
        raise GitRequestError(f"unknown object-format {_show(value)}")
    return hex_len


# --- protocol v2 -------------------------------------------------------------------------


def _check_v2(packets: list[_Packet]) -> None:
    """Check a v2 command request: capabilities, delim-pkt, arguments, flush-pkt."""
    first = packets[0]
    if isinstance(first, bytes) and first.startswith(b"want "):
        raise GitRequestError(
            "body looks like a protocol v0/v1 request, but the Git-Protocol header selects "
            "version 2"
        )
    command: bytes | None = None
    hex_len = _SHA1_HEX
    seen: set[bytes] = set()
    index = 0
    # Real clients: git sends the capabilities before "command=bundle-uri"
    # (get_remote_bundle_uri in connect.c), so the command line may come anywhere in the
    # capability list, as git's server allows.
    while index < len(packets):
        packet = packets[index]
        if not isinstance(packet, bytes):
            break
        key, equals, value = packet.partition(b"=")
        if not equals:
            raise GitRequestError(f"unknown capability {_show(packet)}")
        if key != b"server-option":
            _once(seen, key)
        if key == b"command":
            if value not in _V2_COMMANDS:
                raise GitRequestError(f"unknown command {_show(value)}")
            command = value
        elif key == b"agent":
            _check_bounded(value, _AGENT_RE, MAX_AGENT_BYTES, "agent")
        elif key == b"object-format":
            hex_len = _object_format(value)
        elif key == b"session-id":
            _check_bounded(value, _SESSION_ID_RE, MAX_SESSION_ID_BYTES, "session-id")
        elif key == b"server-option":
            _check_bounded(value, _SERVER_OPTION_RE, MAX_SERVER_OPTION_BYTES, "server-option")
        elif key == b"promisor-remote":
            _check_bounded(value, _PROMISOR_REMOTE_RE, MAX_PROMISOR_REMOTE_BYTES, "promisor-remote")
        else:
            raise GitRequestError(f"unknown capability {_show(packet)}")
        index += 1
    if command is None:
        raise GitRequestError("protocol v2 request has no command= line")
    if index == len(packets) or packets[index] is not _Control.DELIM:
        found = "the end of the body" if index == len(packets) else _describe(packets[index])
        raise GitRequestError(f"expected a delim-pkt after the capabilities, got {found}")
    index += 1
    arguments: list[bytes] = []
    while index < len(packets):
        packet = packets[index]
        if not isinstance(packet, bytes):
            break
        arguments.append(packet)
        index += 1
    if index == len(packets) or packets[index] is not _Control.FLUSH:
        found = "the end of the body" if index == len(packets) else _describe(packets[index])
        raise GitRequestError(f"expected a flush-pkt after the arguments, got {found}")
    if index + 1 != len(packets):
        raise GitRequestError("data after the end of the request")
    _V2_COMMANDS[command](arguments, hex_len)


def _check_flag(keyword: bytes, value: bytes | None, seen: set[bytes]) -> None:
    if value is not None:
        raise GitRequestError(f"{_name(keyword)!r} takes no value")
    _once(seen, keyword)


def _require_value(keyword: bytes, value: bytes | None) -> bytes:
    if value is None:
        raise GitRequestError(f"{_name(keyword)!r} needs a value")
    return value


def _check_ls_refs(arguments: list[bytes], hex_len: int) -> None:
    seen: set[bytes] = set()
    for line in arguments:
        keyword, value = _keyword(line)
        if keyword in _LS_REFS_FLAGS:
            _check_flag(keyword, value, seen)
        elif keyword == b"ref-prefix":
            _check_ref(_require_value(keyword, value), "ref-prefix", allow_empty=True)
        else:
            raise GitRequestError(f"unknown ls-refs argument {_show(line)}")


def _check_fetch(arguments: list[bytes], hex_len: int) -> None:
    # Real clients: go-git sends want, have and done before the other arguments, so the
    # arguments may come in any order, as git's server allows.
    oids = _ObjectFormat(hex_len)
    seen: set[bytes] = set()
    wants = 0
    for line in arguments:
        keyword, value = _keyword(line)
        if keyword in _FETCH_FLAGS:
            _check_flag(keyword, value, seen)
        elif keyword in (b"want", b"have", b"shallow"):
            oids.check(_require_value(keyword, value), _name(keyword))
            if keyword == b"want":
                wants += 1
        elif keyword == b"want-ref":
            _check_ref(_require_value(keyword, value), "want-ref")
            wants += 1
        elif keyword == b"deepen":
            _once(seen, keyword)
            _check_depth(_require_value(keyword, value))
        elif keyword == b"deepen-since":
            _once(seen, keyword)
            _check_timestamp(_require_value(keyword, value))
        elif keyword == b"deepen-not":
            _check_ref(_require_value(keyword, value), "deepen-not ref")
        elif keyword == b"filter":
            _once(seen, keyword)
            _check_filter(_require_value(keyword, value))
        elif keyword == b"packfile-uris":
            _once(seen, keyword)
            _check_bounded(
                _require_value(keyword, value),
                _URI_PROTOCOLS_RE,
                _MAX_URI_PROTOCOLS_BYTES,
                "packfile-uris",
            )
        else:
            raise GitRequestError(f"unknown fetch argument {_show(line)}")
    if not wants and b"wait-for-done" not in seen:
        # Only push negotiation (wait-for-done) sends haves without wants.
        raise GitRequestError("fetch request has no want or want-ref line")


def _check_object_info(arguments: list[bytes], hex_len: int) -> None:
    oids = _ObjectFormat(hex_len)
    seen: set[bytes] = set()
    count = 0
    for line in arguments:
        keyword, value = _keyword(line)
        if keyword in _OBJECT_INFO_FLAGS:
            _check_flag(keyword, value, seen)
        elif keyword == b"oid":
            oids.check(_require_value(keyword, value), "oid")
            count += 1
        else:
            raise GitRequestError(f"unknown object-info argument {_show(line)}")
    if not count:
        raise GitRequestError("object-info request has no oid line")


def _check_bundle_uri(arguments: list[bytes], hex_len: int) -> None:
    if arguments:
        raise GitRequestError(f"unknown bundle-uri argument {_show(arguments[0])}")


_V2_COMMANDS: Final[Mapping[bytes, Callable[[list[bytes], int], None]]] = {
    b"ls-refs": _check_ls_refs,
    b"fetch": _check_fetch,
    b"object-info": _check_object_info,
    b"bundle-uri": _check_bundle_uri,
}
