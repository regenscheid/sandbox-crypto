# SPDX-License-Identifier: MIT
"""Tests for the git upload-pack request check (docs/POLICY.md, "Residual risks").

Every body in ``fixtures/git`` must pass: most were captured from git 2.53 cloning and
fetching local test repositories through ``git http-backend`` (protocol v0, v1 and v2,
gzip, shallow, partial, SHA-256, ls-remote, push negotiation, the large-request probe and
more), and a few follow other clients' source (libgit2, dulwich, go-git) and were accepted
by git's upload-pack. ``fixtures/git/manifest.json`` records the command behind each.

The negative tests build requests from those shapes and break one rule at a time:
framing, dialect, keywords, object IDs, value bounds and character sets, gzip handling,
and data after the end of a valid request.
"""

from __future__ import annotations

import gzip
import io
import json
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.policy_middleware import gitproto
from sandbox_science.policy_middleware.gitproto import (
    MAX_AGENT_BYTES,
    MAX_DECODED_BYTES,
    MAX_FILTER_BYTES,
    MAX_PKT_LEN,
    MAX_PROMISOR_REMOTE_BYTES,
    MAX_REF_BYTES,
    MAX_SERVER_OPTION_BYTES,
    MAX_SESSION_ID_BYTES,
    GitRequestError,
    check_upload_pack_request,
)

FIXTURES = Path(__file__).parent / "fixtures" / "git"

OID = b"4ac2caca3005806f98ca60c0db850221e46fc5b0"
OID2 = b"514f82abf6dbba5d0ee36823791cb8ef870f2549"
OID256 = b"d22af8cbeb33b675da7e2f569057ab55bda665f8416ea3dccf5e288dfc00533d"
FLUSH = b"0000"
DELIM = b"0001"
V0_CAPS = b"multi_ack_detailed no-done side-band-64k thin-pack ofs-delta agent=git/2.53.0-Linux"


def pkt(line: bytes) -> bytes:
    """Frame one pkt-line."""
    return b"%04x" % (len(line) + 4) + line


def v0_request(
    *options: bytes,
    capabilities: bytes = V0_CAPS,
    wants: tuple[bytes, ...] = (OID2,),
    haves: tuple[bytes, ...] = (),
    end: bytes | None = pkt(b"done\n"),
) -> bytes:
    """A stateless v0 request: wants, options, flush, then haves and ``end``."""
    first = b"want " + OID + (b" " + capabilities if capabilities else b"") + b"\n"
    body = pkt(first) + b"".join(pkt(b"want " + oid + b"\n") for oid in wants)
    body += b"".join(pkt(option) for option in options) + FLUSH
    if end is not None:
        body += b"".join(pkt(b"have " + oid + b"\n") for oid in haves) + end
    return body


V2_CAPS = (b"agent=git/2.53.0-Linux", b"object-format=sha1")


def v2_request(
    *arguments: bytes, command: bytes = b"fetch", capabilities: tuple[bytes, ...] = V2_CAPS
) -> bytes:
    """A v2 command request: command, capabilities, delim, arguments, flush."""
    head = pkt(b"command=" + command) + b"".join(pkt(cap) for cap in capabilities)
    return head + DELIM + b"".join(pkt(argument) for argument in arguments) + FLUSH


def fetch(*arguments: bytes, capabilities: tuple[bytes, ...] = V2_CAPS) -> bytes:
    """A v2 fetch of OID with the given extra arguments."""
    return v2_request(
        b"thin-pack", b"ofs-delta", b"want " + OID + b"\n", *arguments, capabilities=capabilities
    )


def check_v0(body: bytes, **kwargs: Any) -> None:
    check_upload_pack_request(body, **kwargs)


def check_v2(body: bytes, **kwargs: Any) -> None:
    check_upload_pack_request(body, git_protocol="version=2", **kwargs)


def rejects(check: Callable[[bytes], None], body: bytes, match: str | None = None) -> None:
    with pytest.raises(GitRequestError, match=match):
        check(body)


# --- fixtures ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Fixture:
    file: str
    origin: str
    command: str
    content_encoding: str | None
    git_protocol: str | None
    expect: str

    @property
    def body(self) -> bytes:
        return (FIXTURES / self.file).read_bytes()

    @property
    def decoded(self) -> bytes:
        body = self.body
        return gzip.decompress(body) if body.startswith(b"\x1f\x8b") else body

    def check(self, body: bytes | None = None, **overrides: Any) -> None:
        options: dict[str, Any] = {
            "content_encoding": self.content_encoding,
            "git_protocol": self.git_protocol,
        }
        options.update(overrides)
        check_upload_pack_request(self.body if body is None else body, **options)


def load_fixtures() -> list[Fixture]:
    manifest = cast("dict[str, Any]", json.loads((FIXTURES / "manifest.json").read_text()))
    entries = cast("list[dict[str, Any]]", manifest["fixtures"])
    return [
        Fixture(
            file=entry["file"],
            origin=entry["origin"],
            command=entry["command"],
            content_encoding=entry["content_encoding"],
            git_protocol=entry["git_protocol"],
            expect=entry["expect"],
        )
        for entry in entries
    ]


ALL_FIXTURES = load_fixtures()
ACCEPTED = [fixture for fixture in ALL_FIXTURES if fixture.expect == "accept"]
GZIPPED = [fixture for fixture in ACCEPTED if fixture.content_encoding == "gzip"]
NOT_PROBES = [fixture for fixture in ACCEPTED if fixture.decoded != FLUSH]


def ids(fixtures: list[Fixture]) -> list[str]:
    return [fixture.file.removesuffix(".bin") for fixture in fixtures]


def test_manifest_lists_every_fixture_file() -> None:
    listed = {fixture.file for fixture in ALL_FIXTURES}
    assert listed == {path.name for path in FIXTURES.glob("*.bin")}
    assert len(listed) == len(ALL_FIXTURES)


def test_fixtures_cover_the_required_git_operations() -> None:
    """Captures exist for each operation the check must never break."""
    captured = [fixture for fixture in ACCEPTED if fixture.origin == "captured"]
    names = {fixture.file for fixture in captured}
    for prefix in (
        "clone-v0",
        "clone-v1",
        "clone-v2",
        "fetch-haves-v0",
        "fetch-haves-v2",
        "shallow-clone-v0",
        "shallow-clone-v2",
        "partial-blob-none-v0",
        "partial-blob-none-v2",
        "ls-remote-v2",
        "sha256-clone-v0",
        "sha256-clone-v2",
        "large-v0-clone-probe",
        "push-negotiate-v2",
    ):
        assert any(name.startswith(prefix + "-") for name in names), prefix
    # git gzips requests over 1024 bytes; both dialects and SHA-256 are covered.
    gzip_protocols = {fixture.git_protocol for fixture in GZIPPED if fixture.origin == "captured"}
    assert gzip_protocols == {None, "version=1", "version=2"}
    assert any(fixture.file.startswith("sha256-") for fixture in GZIPPED)
    # The probe git sends before a request larger than http.postBuffer.
    assert any(fixture.decoded == FLUSH and fixture.git_protocol is None for fixture in captured)


@pytest.mark.parametrize("fixture", ACCEPTED, ids=ids(ACCEPTED))
def test_real_client_requests_pass(fixture: Fixture) -> None:
    fixture.check()


@pytest.mark.parametrize("fixture", GZIPPED, ids=ids(GZIPPED))
def test_gzip_requests_pass_without_content_encoding_header(fixture: Fixture) -> None:
    """The proxy may not show Content-Encoding; the gzip magic bytes are enough."""
    fixture.check(content_encoding=None)
    fixture.check(content_encoding="x-gzip")


@pytest.mark.parametrize("fixture", GZIPPED, ids=ids(GZIPPED))
def test_gzip_requests_pass_decoded(fixture: Fixture) -> None:
    fixture.check(fixture.decoded, content_encoding=None)


def test_captured_c_quoted_deepen_not_is_rejected() -> None:
    """git's v0 HTTP client C-quotes a non-ASCII --shallow-exclude ref, and git's own
    upload-pack rejects the result as "not a ref", so nothing that works is lost."""
    (fixture,) = [fixture for fixture in ALL_FIXTURES if fixture.expect == "reject"]
    with pytest.raises(GitRequestError, match="deepen-not"):
        fixture.check()


@pytest.mark.parametrize("fixture", NOT_PROBES, ids=ids(NOT_PROBES))
def test_requests_are_rejected_in_the_other_dialect(fixture: Fixture) -> None:
    other = None if fixture.git_protocol == "version=2" else "version=2"
    with pytest.raises(GitRequestError):
        fixture.check(git_protocol=other)


JUNK = (b"x", b"\n", b"00", b"0001", pkt(b"junk"), pkt(b"exfiltrated data\n"))


@pytest.mark.parametrize("fixture", ACCEPTED, ids=ids(ACCEPTED))
@pytest.mark.parametrize("junk", JUNK)
def test_valid_requests_followed_by_junk_are_rejected(fixture: Fixture, junk: bytes) -> None:
    with pytest.raises(GitRequestError):
        fixture.check(fixture.decoded + junk, content_encoding=None)


@pytest.mark.parametrize("fixture", ACCEPTED, ids=ids(ACCEPTED))
def test_truncated_requests_are_rejected(fixture: Fixture) -> None:
    with pytest.raises(GitRequestError):
        fixture.check(fixture.body[:-1])


@pytest.mark.parametrize("fixture", GZIPPED, ids=ids(GZIPPED))
def test_gzip_requests_with_junk_inside_are_rejected(fixture: Fixture) -> None:
    with pytest.raises(GitRequestError):
        fixture.check(gzip.compress(fixture.decoded + pkt(b"junk"), mtime=0))


# --- framing -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (b"", "empty request body"),
        (b"zzzz", "bad pkt-line length"),
        (b"00g0" + b"x" * 12, "bad pkt-line length"),
        (b"000", "bad pkt-line length"),
        (v0_request() + b"00", "bad pkt-line length"),
        (b"0002", "response-end"),
        (b"0003", "0003"),
        (b"0004", "empty pkt-line"),
        (pkt(b"want " + OID)[:-3], "truncated pkt-line"),
        (b"fff1" + b"a" * (0xFFF1 - 4), "exceeds"),
        (b"ffff" + b"a" * (0xFFFF - 4), "exceeds"),
    ],
)
def test_bad_framing_is_rejected(body: bytes, match: str) -> None:
    rejects(check_v0, body, match)
    rejects(check_v2, body, match)


def test_upper_case_pkt_length_is_rejected() -> None:
    """git's reader accepts upper-case hex lengths, but no client writes them."""
    body = v0_request(b"deepen 1")
    assert b"000cdeepen 1" in body
    check_v0(body)
    rejects(check_v0, body.replace(b"000cdeepen 1", b"000Cdeepen 1"), "bad pkt-line length")


def test_maximum_pkt_line_length_is_enforced_exactly() -> None:
    """A 65520-byte pkt-line is framed correctly (and then judged on content)."""
    longest = b"%04x" % MAX_PKT_LEN + b"want " + b"a" * (MAX_PKT_LEN - 9)
    rejects(check_v0, longest, "invalid object ID")
    rejects(check_v0, b"%04x" % (MAX_PKT_LEN + 1) + b"a" * (MAX_PKT_LEN - 3), "exceeds")


@pytest.mark.parametrize("char", [b"\x00", b"\r", b"\t", b"\x1b", b"\x7f", b"\x0b", b"\x0c"])
def test_control_characters_are_rejected(char: bytes) -> None:
    rejects(check_v0, v0_request(b"deepen 1" + char), "control character")
    rejects(check_v2, fetch(b"have " + OID + char + b"\n"), "control character")
    rejects(check_v2, v2_request(command=b"ls-refs", capabilities=(b"agent=git" + char,)))


def test_line_feed_only_ends_a_payload() -> None:
    rejects(check_v0, pkt(b"want " + OID + b"\nhave " + OID2 + b"\n") + FLUSH, "line feed")
    rejects(check_v0, v0_request(b"deepen 1\n\n"), "line feed")
    check_v0(v0_request(b"deepen 1\n"))
    check_v0(v0_request(b"deepen 1"))


# --- arbitrary payloads ------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"hello world",
        b'{"query": "SELECT * FROM secrets", "token": "test-secret-123"}',
        b"[1, 2, 3]",
        b"a=1&b=2",
        b"POST /upload HTTP/1.1\r\nHost: example.org\r\n\r\n",
        b"\x89PNG\r\n\x1a\n",
        b"PK\x03\x04",
        bytes(range(256)),
        pkt(b'{"exfil": "test-secret-123"}') + FLUSH,
        pkt(b"dGVzdC1zZWNyZXQtMTIz") + FLUSH,
        FLUSH + pkt(b"test-secret-123"),
        zlib.compress(v0_request()),
    ],
)
def test_arbitrary_payloads_are_rejected(body: bytes) -> None:
    rejects(check_v0, body)
    rejects(check_v2, body)
    rejects(lambda b: check_upload_pack_request(b, content_encoding="gzip"), body)


@pytest.mark.parametrize("count", [2, 3, 100])
def test_bodies_of_only_flushes_are_rejected(count: int) -> None:
    rejects(check_v0, FLUSH * count)
    rejects(check_v2, FLUSH * count)


@pytest.mark.parametrize("git_protocol", [None, "version=0", "version=1", "version=2"])
def test_single_flush_probe_is_accepted(git_protocol: str | None) -> None:
    """remote-curl.c probe_rpc POSTs "0000" (without Git-Protocol) before a large request;
    in v2 a lone flush-pkt is also the empty request."""
    check_upload_pack_request(FLUSH, git_protocol=git_protocol)


# --- protocol v0 and v1 ------------------------------------------------------------------


def test_v0_request_shapes() -> None:
    check_v0(v0_request())  # clone: wants, flush, done
    check_v0(v0_request(end=None))  # want list alone, sent before a deepen
    check_v0(v0_request(haves=(OID, OID2), end=FLUSH))  # one negotiation round
    check_v0(v0_request(haves=(OID,), end=pkt(b"done")))  # LF is optional
    check_v0(v0_request(wants=()))
    check_v0(v0_request(wants=(OID, OID)))  # clones repeat the object HEAD points to
    check_v0(v0_request(capabilities=b""))


@pytest.mark.parametrize("git_protocol", [None, "version=0", "version=1", " version=1 "])
def test_v0_grammar_applies_without_git_protocol_version_2(git_protocol: str | None) -> None:
    check_upload_pack_request(v0_request(), git_protocol=git_protocol)
    with pytest.raises(GitRequestError, match="looks like a protocol v2 request"):
        check_upload_pack_request(fetch(), git_protocol=git_protocol)


def test_v0_options() -> None:
    check_v0(
        v0_request(
            b"shallow " + OID,
            b"deepen 2147483647",
            b"deepen-since 1700001000",
            b"deepen-not v1.0",
            b"deepen-not refs/heads/f\xc3\xbcnf",
            b"filter combine:blob:none+tree:3",
            end=None,
        )
    )


@pytest.mark.parametrize(
    "lines",
    [
        (b"deepen 1", b"deepen 2"),
        (b"deepen-since 1", b"deepen-since 2"),
        (b"filter blob:none", b"filter tree:0"),
    ],
)
def test_v0_single_options_may_not_repeat(lines: tuple[bytes, ...]) -> None:
    rejects(check_v0, v0_request(*lines), "more than once")


def test_v0_libgit2_trailing_space_is_accepted_once() -> None:
    """libgit2 and dulwich end the capability list with a space."""
    check_v0(v0_request(capabilities=b"multi_ack_detailed side-band-64k ofs-delta "))
    check_v0(pkt(b"want " + OID + b" \n") + FLUSH + pkt(b"done\n"))
    rejects(check_v0, pkt(b"want " + OID + b"  \n") + FLUSH, "unknown capability")
    rejects(check_v0, v0_request(capabilities=b"ofs-delta  "), "unknown capability")
    rejects(check_v0, v0_request(capabilities=b"ofs-delta  thin-pack"), "unknown capability")


@pytest.mark.parametrize(
    ("capabilities", "match"),
    [
        (b"thin-pack thin-pack", "more than once"),
        (b"agent=a agent=b", "more than once"),
        (b"exfil", "unknown capability"),
        (b"symref=HEAD:refs/heads/main", "unknown capability"),
        (b"report-status", "unknown capability"),
        (b"allow-tip-sha1-in-want", "unknown capability"),
        (b"agent", "unknown capability"),
        (b"thin-pack=1", "unknown capability"),
        (b"agent=", "invalid agent"),
        (b"agent=" + b"a" * (MAX_AGENT_BYTES + 1), "longer than"),
        (b"agent=git\xc3\xa9", "invalid agent"),
        (b"session-id=" + b"s" * (MAX_SESSION_ID_BYTES + 1), "longer than"),
        (b"object-format=md5", "unknown object-format"),
        (b"object-format=sha256", "invalid object ID"),
    ],
)
def test_v0_bad_capabilities_are_rejected(capabilities: bytes, match: str) -> None:
    rejects(check_v0, v0_request(capabilities=capabilities), match)


def test_v0_capability_bounds() -> None:
    check_v0(v0_request(capabilities=b"agent=" + b"a" * MAX_AGENT_BYTES))
    check_v0(v0_request(capabilities=b"session-id=" + b"s" * MAX_SESSION_ID_BYTES))


def test_v0_capabilities_only_on_the_first_want() -> None:
    body = pkt(b"want " + OID + b"\n") + pkt(b"want " + OID2 + b" thin-pack\n") + FLUSH
    rejects(check_v0, body, "invalid object ID")


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (v0_request(b"deepen 1", b"want " + OID), "must come before"),
        (v0_request(b"have " + OID), "unexpected line"),
        (v0_request(b"done"), "unexpected line"),
        (v0_request(b"thin-pack"), "unexpected line"),
        (v0_request(b"exfil " + OID), "unexpected line"),
        (v0_request(b"command=fetch"), "unexpected line"),
        (v0_request(haves=(OID,), end=pkt(b"note test-secret")), "expected have, done"),
        (v0_request(haves=(OID,), end=b""), "must end with a flush-pkt or done"),
        (v0_request(haves=(OID,), end=pkt(b"done") + pkt(b"done")), "after the end"),
        (v0_request(haves=(OID,), end=FLUSH + pkt(b"have " + OID)), "after the end"),
        (v0_request(end=DELIM), "expected have, done"),
        (pkt(b"want " + OID), "must end with a flush-pkt"),
        (pkt(b"want " + OID) + DELIM, "must end with a flush-pkt"),
        (pkt(b"have " + OID) + FLUSH, "must start with a want line"),
        (pkt(b"done") + FLUSH, "must start with a want line"),
        (FLUSH + pkt(b"want " + OID) + FLUSH, "must start with a want line"),
        (pkt(b"command=fetch") + DELIM + FLUSH, "looks like a protocol v2 request"),
    ],
)
def test_v0_structure_violations_are_rejected(body: bytes, match: str) -> None:
    rejects(check_v0, body, match)


# --- object IDs --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "oid",
    [
        OID.upper(),
        OID[:-1],
        OID + b"0",
        OID[:-1] + b"g",
        OID[:-1] + b" ",
        b"",
        OID256[:-1],
        b"0x" + OID[2:],
        OID + b"\xc3\xa9",
    ],
)
def test_bad_object_ids_are_rejected(oid: bytes) -> None:
    rejects(check_v0, v0_request(wants=(oid,)), "object ID")
    rejects(check_v0, v0_request(haves=(oid,), end=FLUSH), "object ID")
    rejects(check_v0, v0_request(b"shallow " + oid), "object ID")
    rejects(check_v2, fetch(b"have " + oid), "object ID")
    rejects(check_v2, fetch(b"shallow " + oid), "object ID")
    rejects(check_v2, v2_request(b"oid " + oid, command=b"object-info"), "object ID")


def test_object_id_length_follows_the_object_format() -> None:
    sha256 = (b"agent=git/2.53.0-Linux", b"object-format=sha256")
    check_v2(v2_request(b"want " + OID256, b"done", capabilities=sha256))
    rejects(check_v2, v2_request(b"want " + OID, b"done", capabilities=sha256), "object ID")
    rejects(check_v2, fetch(b"have " + OID256), "object ID")
    rejects(check_v2, v2_request(b"want " + OID256, b"done", capabilities=()), "object ID")
    # v0 does not name the format: every object ID must have the first one's length.
    check_v0(pkt(b"want " + OID256 + b" ofs-delta\n") + FLUSH + pkt(b"have " + OID256) + FLUSH)
    rejects(check_v0, v0_request(wants=(OID256,)), "object ID")
    rejects(check_v0, v0_request(haves=(OID256,), end=FLUSH), "object ID")
    check_v0(v0_request(capabilities=b"object-format=sha1 ofs-delta"))


# --- protocol v2 -------------------------------------------------------------------------


def test_v2_request_shapes() -> None:
    check_v2(fetch(b"done"))
    check_v2(fetch(b"have " + OID2, b"have " + OID))  # a negotiation round
    check_v2(v2_request(b"peel", b"symrefs", b"unborn", b"ref-prefix HEAD", command=b"ls-refs"))
    check_v2(v2_request(command=b"ls-refs"))
    check_v2(v2_request(command=b"bundle-uri"))
    check_v2(v2_request(b"size", b"type", b"oid " + OID, command=b"object-info"))
    check_v2(v2_request(b"wait-for-done", b"have " + OID))  # push negotiation
    check_v2(fetch(capabilities=()))
    check_v2(
        fetch(
            b"want " + OID,  # clones repeat the object HEAD points to
            b"want-ref refs/heads/main",
            b"want-ref HEAD",
            b"no-progress",
            b"include-tag",
            b"sideband-all",
            b"shallow " + OID2,
            b"deepen 3",
            b"deepen-relative",
            b"deepen-since 0",
            b"deepen-not v1.0",
            b"filter blob:limit=1024",
            b"packfile-uris http,https",
            b"have " + OID2,
            b"done",
        )
    )


def test_v2_capabilities_may_surround_the_command() -> None:
    """git sends the capabilities before command=bundle-uri (connect.c)."""
    body = pkt(b"agent=git/2.53.0-Linux") + pkt(b"object-format=sha1")
    check_v2(body + pkt(b"command=bundle-uri\n") + DELIM + FLUSH)


def test_v2_arguments_may_come_in_any_order() -> None:
    """go-git sends want, have and done before the other arguments."""
    check_v2(
        v2_request(
            b"want " + OID, b"have " + OID2, b"done", b"thin-pack", b"ofs-delta", b"deepen 2"
        )
    )


@pytest.mark.parametrize(
    ("capability", "valid"),
    [
        (b"server-option=trace=1", True),
        (b"server-option=x y", True),
        (b"server-option=", True),
        (b"server-option=" + b"o" * MAX_SERVER_OPTION_BYTES, True),
        (b"server-option=" + b"o" * (MAX_SERVER_OPTION_BYTES + 1), False),
        (b"server-option=caf\xc3\xa9", False),
        (b"session-id=20261010T235427.816521Z-H00000000-P001cb08f", True),
        (b"session-id=" + b"s" * MAX_SESSION_ID_BYTES, True),
        (b"session-id=" + b"s" * (MAX_SESSION_ID_BYTES + 1), False),
        (b"session-id=a b", False),
        (b"session-id=", False),
        (b"promisor-remote=lop", True),
        (b"promisor-remote=lop;other%3bname", True),
        (b"promisor-remote=" + b"p" * MAX_PROMISOR_REMOTE_BYTES, True),
        (b"promisor-remote=" + b"p" * (MAX_PROMISOR_REMOTE_BYTES + 1), False),
        (b"promisor-remote=a,b", False),
        (b"promisor-remote=a;", False),
        (b"promisor-remote=%zz", False),
        (b"promisor-remote=", False),
        (b"agent=" + b"a" * MAX_AGENT_BYTES, True),
        (b"agent=" + b"a" * (MAX_AGENT_BYTES + 1), False),
        (b"agent=my agent", False),
        (b"agent=", False),
        (b"agent", False),
        (b"object-format=sha256", False),  # OID is a SHA-1 object ID
        (b"object-format=sha3", False),
        (b"exfil=test-secret", False),
        (b"command", False),
        (b"ls-refs", False),
        (b"fetch=shallow", False),
    ],
)
def test_v2_capability_lines(capability: bytes, valid: bool) -> None:
    body = fetch(b"done", capabilities=(b"agent=git/2.53.0-Linux", capability))
    if capability.startswith(b"agent"):
        body = fetch(b"done", capabilities=(capability,))
    if valid:
        check_v2(body)
    else:
        rejects(check_v2, body)


@pytest.mark.parametrize(
    "capabilities",
    [
        (b"agent=a", b"agent=b"),
        (b"object-format=sha1", b"object-format=sha1"),
        (b"session-id=a", b"session-id=b"),
        (b"promisor-remote=a", b"promisor-remote=b"),
        (b"command=fetch",),
    ],
)
def test_v2_single_capabilities_may_not_repeat(capabilities: tuple[bytes, ...]) -> None:
    rejects(check_v2, fetch(b"done", capabilities=capabilities), "more than once")


def test_v2_server_options_may_repeat() -> None:
    check_v2(fetch(b"done", capabilities=(b"server-option=a", b"server-option=a")))


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (pkt(b"agent=git") + DELIM + FLUSH, "no command"),
        (DELIM + pkt(b"want " + OID) + FLUSH, "no command"),
        (v2_request(command=b"push"), "unknown command"),
        (v2_request(command=b"receive-pack"), "unknown command"),
        (v2_request(command=b"fetch=x"), "unknown command"),
        (v2_request(command=b""), "unknown command"),
        (pkt(b"command=fetch") + pkt(b"want " + OID) + DELIM + FLUSH, "unknown capability"),
        (pkt(b"command=ls-refs") + FLUSH, "expected a delim-pkt"),
        (pkt(b"command=ls-refs"), "expected a delim-pkt"),
        (pkt(b"command=ls-refs") + DELIM + DELIM + FLUSH, "expected a flush-pkt"),
        (pkt(b"command=ls-refs") + DELIM + pkt(b"peel"), "expected a flush-pkt"),
        (v2_request(command=b"ls-refs") + FLUSH, "after the end"),
        (v2_request(command=b"ls-refs") + v2_request(command=b"ls-refs"), "after the end"),
        (v2_request(command=b"ls-refs") + pkt(b"peel"), "after the end"),
        (v0_request(), "looks like a protocol v0/v1 request"),
    ],
)
def test_v2_structure_violations_are_rejected(body: bytes, match: str) -> None:
    rejects(check_v2, body, match)


@pytest.mark.parametrize(
    ("command", "argument"),
    [
        (b"ls-refs", b"want " + OID),
        (b"ls-refs", b"peel yes"),
        (b"ls-refs", b"ref-prefix"),
        (b"ls-refs", b"exfil test-secret"),
        (b"fetch", b"peel"),
        (b"fetch", b"ref-prefix refs/"),
        (b"fetch", b"thin-pack yes"),
        (b"fetch", b"want"),
        (b"fetch", b"have"),
        (b"fetch", b"filter"),
        (b"fetch", b"deepen"),
        (b"fetch", b"exfil test-secret"),
        (b"fetch", b"ERR test-secret"),
        (b"object-info", b"want " + OID),
        (b"object-info", b"size 10"),
        (b"object-info", b"oid"),
        (b"bundle-uri", b"peel"),
        (b"bundle-uri", b"want " + OID),
    ],
)
def test_v2_commands_reject_unknown_arguments(command: bytes, argument: bytes) -> None:
    valid = {
        b"ls-refs": (b"peel",),
        b"fetch": (b"want " + OID2, b"done"),
        b"object-info": (b"oid " + OID2,),
        b"bundle-uri": (),
    }[command]
    check_v2(v2_request(*valid, command=command))
    rejects(check_v2, v2_request(*valid, argument, command=command))


@pytest.mark.parametrize(
    ("command", "arguments"),
    [
        (b"ls-refs", (b"peel", b"peel")),
        (b"ls-refs", (b"unborn", b"unborn")),
        (b"fetch", (b"want " + OID, b"done", b"done")),
        (b"fetch", (b"want " + OID, b"thin-pack", b"thin-pack")),
        (b"fetch", (b"want " + OID, b"deepen 1", b"deepen 2")),
        (b"fetch", (b"want " + OID, b"deepen-since 1", b"deepen-since 1")),
        (b"fetch", (b"want " + OID, b"filter blob:none", b"filter blob:none")),
        (b"fetch", (b"want " + OID, b"packfile-uris https", b"packfile-uris http")),
        (b"object-info", (b"size", b"size", b"oid " + OID)),
    ],
)
def test_v2_single_arguments_may_not_repeat(command: bytes, arguments: tuple[bytes, ...]) -> None:
    rejects(check_v2, v2_request(*arguments, command=command), "more than once")


def test_v2_fetch_needs_a_want_unless_negotiating_for_push() -> None:
    rejects(check_v2, v2_request(b"have " + OID, b"done"), "no want")
    rejects(check_v2, v2_request(b"thin-pack"), "no want")
    rejects(check_v2, v2_request(), "no want")
    check_v2(v2_request(b"want-ref refs/heads/main", b"done"))
    check_v2(v2_request(b"wait-for-done"))


def test_v2_object_info_needs_an_oid() -> None:
    rejects(check_v2, v2_request(b"size", command=b"object-info"), "no oid")
    rejects(check_v2, v2_request(command=b"object-info"), "no oid")


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        (b"https", True),
        (b"http,https", True),
        (b"https,https", True),
        (b"ftp", False),
        (b"https,", False),
        (b"https,file", False),
        (b"", False),
        (b"https" + b",https" * 20, False),
    ],
)
def test_v2_packfile_uris(value: bytes, valid: bool) -> None:
    body = fetch(b"done", b"packfile-uris " + value)
    if valid:
        check_v2(body)
    else:
        rejects(check_v2, body)


# --- values ------------------------------------------------------------------------------


REF_ACCEPTED = [
    b"refs/heads/main",
    b"HEAD",
    b"v1.0",
    b"refs/heads/f\xc3\xbcnf",
    b"dir/na\xc3\xafve-\xc5\x9d",
    b"refs/heads/feature-",
    b"a" * MAX_REF_BYTES,
]
REF_REJECTED = [
    b"a" * (MAX_REF_BYTES + 1),
    b"refs/heads/a b",
    b"a~1",
    b"a^{}",
    b"a:b",
    b"a?",
    b"refs/heads/*",
    b"a[0]",
    b"a\\b",
    b'"f\\303\\274nf"',
    b"\xff\xfe",
    b"a\xc2\x85b",  # U+0085, a C1 control character
]


@pytest.mark.parametrize("ref", REF_ACCEPTED)
def test_ref_values_are_accepted(ref: bytes) -> None:
    check_v2(fetch(b"want-ref " + ref, b"deepen-not " + ref))
    check_v2(v2_request(b"ref-prefix " + ref, command=b"ls-refs"))
    check_v0(v0_request(b"deepen-not " + ref, end=None))


@pytest.mark.parametrize("ref", REF_REJECTED)
def test_bad_ref_values_are_rejected(ref: bytes) -> None:
    rejects(check_v2, fetch(b"want-ref " + ref))
    rejects(check_v2, fetch(b"deepen-not " + ref))
    rejects(check_v2, v2_request(b"ref-prefix " + ref, command=b"ls-refs"))
    rejects(check_v0, v0_request(b"deepen-not " + ref, end=None))


def test_non_breaking_space_is_allowed_in_refs() -> None:
    """Only control characters and git's forbidden ASCII are excluded from UTF-8 refs."""
    check_v2(v2_request(b"ref-prefix a\xc2\xa0", command=b"ls-refs"))


def test_empty_ref_prefix_is_accepted_but_empty_refs_are_not() -> None:
    """git sends "ref-prefix " for a refspec that starts with a glob ("*:refs/x/*")."""
    check_v2(v2_request(b"ref-prefix ", command=b"ls-refs"))
    rejects(check_v2, fetch(b"want-ref "))
    rejects(check_v2, fetch(b"deepen-not "))
    rejects(check_v0, v0_request(b"deepen-not ", end=None))


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        (b"1", True),
        (b"2147483647", True),
        (b"0", False),
        (b"2147483648", False),
        (b"-1", False),
        (b"01", False),
        (b"0x10", False),
        (b"1e3", False),
        (b" 1", False),
        (b"", False),
    ],
)
def test_deepen_depth(value: bytes, valid: bool) -> None:
    for body, check in (
        (v0_request(b"deepen " + value), check_v0),
        (fetch(b"deepen " + value), check_v2),
    ):
        if valid:
            check(body)
        else:
            rejects(check, body, "deepen")


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        (b"0", True),
        (b"1700001000", True),
        (b"18446744073709551614", True),
        (b"18446744073709551615", False),  # -1, which upload-pack rejects
        (b"99999999999999999999", False),
        (b"-100", False),
        (b"0100", False),
        (b"1.5", False),
        (b"2023-11-14", False),
        (b"", False),
    ],
)
def test_deepen_since_time(value: bytes, valid: bool) -> None:
    line = b"deepen-since " + value
    for body, check in ((v0_request(line), check_v0), (fetch(line), check_v2)):
        if valid:
            check(body)
        else:
            rejects(check, body, "deepen-since")


FILTERS_ACCEPTED = [
    b"blob:none",
    b"blob:limit=0",
    b"blob:limit=1024",
    b"blob:limit=1k",
    b"blob:limit=1M",
    b"tree:0",
    b"tree:3",
    b"tree:1k",
    b"object:type=blob",
    b"object:type=commit",
    b"object:type=tree",
    b"object:type=tag",
    b"sparse:oid=main:main/file1.txt",
    b"sparse:oid=" + OID,
    b"sparse:oid=main:path with space",
    b"combine:blob:none+tree:3",
    b"combine:blob:limit=1m+sparse:oid=main:main/file1.txt+tree:1k",
    b"combine:sparse:oid=main%3apath%20with%20space+tree:0",
    b"combine:combine:blob:none%2btree:1+object:type=tag",  # several --filter options
    b"combine:" + b"+".join([b"tree:1"] * 100),
]
FILTERS_REJECTED = [
    b"",
    b"auto",
    b"BLOB:NONE",
    b"blob:none ",
    b"blob:limit=",
    b"blob:limit=-1",
    b"blob:limit=1x",
    b"blob:limit=0x10",
    b"tree:",
    b"tree:a",
    b"object:type=",
    b"object:type=exfil",
    b"sparse:oid=",
    b"sparse:path=/etc/passwd",
    b"sparse:oid=caf\xc3\xa9",
    b"combine:",
    b"combine:+",
    b"combine:blob:none++tree:1",
    b"combine:blob:none+",
    b"combine:sparse:oid=a~b",
    b"combine:sparse:oid=a b",
    b"combine:blob%3Anone%00",
    b"combine:blob:none%2",
    b"combine:auto",
    b"combine:test-secret",
    b"combine:combine:combine:combine:combine:blob:none",
    b"blob:" + b"n" * MAX_FILTER_BYTES,
    b"sparse:oid=" + b"x" * MAX_FILTER_BYTES,
]


@pytest.mark.parametrize("spec", FILTERS_ACCEPTED)
def test_filter_specs_are_accepted(spec: bytes) -> None:
    check_v0(v0_request(b"filter " + spec))
    check_v2(fetch(b"filter " + spec, b"done"))


@pytest.mark.parametrize("spec", FILTERS_REJECTED)
def test_bad_filter_specs_are_rejected(spec: bytes) -> None:
    rejects(check_v0, v0_request(b"filter " + spec))
    rejects(check_v2, fetch(b"filter " + spec, b"done"))


def test_filter_length_bound() -> None:
    longest = b"sparse:oid=" + b"x" * (MAX_FILTER_BYTES - len(b"sparse:oid="))
    check_v2(fetch(b"filter " + longest, b"done"))
    rejects(check_v2, fetch(b"filter " + longest + b"x", b"done"), "filter spec")


# --- Git-Protocol header -----------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        "version=3",
        "version=2:object-format=sha256",
        "version=1:version=2",
        "version=2, version=2",
        "Version=2",
        "version = 2",
        "version=",
        "",
        "2",
        "version=2\x00",
    ],
)
def test_unsupported_git_protocol_headers_are_rejected(header: str) -> None:
    for body in (FLUSH, v0_request(), fetch(b"done")):
        with pytest.raises(GitRequestError, match="Git-Protocol"):
            check_upload_pack_request(body, git_protocol=header)


def test_git_protocol_header_whitespace_is_ignored() -> None:
    check_upload_pack_request(fetch(b"done"), git_protocol=" version=2\t")


# --- Content-Encoding and gzip -------------------------------------------------------------


def gzip_member(
    data: bytes,
    *,
    flags: int = 0,
    method: int = 8,
    optional: bytes = b"",
    deflate: bytes | None = None,
    crc: int | None = None,
    size: int | None = None,
) -> bytes:
    """Build one gzip member by hand so each field can be broken on its own."""
    if deflate is None:
        compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
        deflate = compressor.compress(data) + compressor.flush()
    header = b"\x1f\x8b" + bytes([method, flags]) + b"\x00\x00\x00\x00\x02\x03" + optional
    trailer = struct.pack(
        "<II",
        zlib.crc32(data) if crc is None else crc,
        len(data) % 2**32 if size is None else size,
    )
    return header + deflate + trailer


def check_gzip(body: bytes, **kwargs: Any) -> None:
    check_upload_pack_request(body, content_encoding="gzip", **kwargs)


GZIP_REQUEST = v0_request(haves=(OID, OID2) * 20, end=FLUSH)


def test_gzip_request_variants_are_accepted() -> None:
    member = gzip_member(GZIP_REQUEST)
    check_gzip(member)
    check_upload_pack_request(member)
    for header in ("x-gzip", "GZIP", " gzip "):
        check_upload_pack_request(member, content_encoding=header)
    check_gzip(gzip.compress(GZIP_REQUEST))  # Python's header has a non-zero MTIME
    check_gzip(gzip_member(fetch(b"done")), git_protocol="version=2")
    check_gzip(gzip_member(FLUSH))


@pytest.mark.parametrize(
    "header", ["deflate", "br", "zstd", "compress", "identity", "gzip, gzip", "gzip,identity", ""]
)
def test_unsupported_content_encodings_are_rejected(header: str) -> None:
    for body in (v0_request(), gzip_member(v0_request())):
        with pytest.raises(GitRequestError, match="Content-Encoding"):
            check_upload_pack_request(body, content_encoding=header)


def test_declared_gzip_needs_gzip_data() -> None:
    rejects(check_gzip, v0_request(), "not gzip data")
    rejects(check_gzip, zlib.compress(v0_request()), "not gzip data")


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (gzip_member(GZIP_REQUEST) + b"x", "after the gzip member"),
        (gzip_member(GZIP_REQUEST) + FLUSH, "after the gzip member"),
        (gzip_member(GZIP_REQUEST) * 2, "after the gzip member"),
        (gzip_member(GZIP_REQUEST)[:-1], "truncated gzip trailer"),
        (gzip_member(GZIP_REQUEST)[:-8], "truncated gzip trailer"),
        (gzip_member(GZIP_REQUEST)[:-20], "truncated deflate data"),
        (gzip_member(GZIP_REQUEST)[:12], "truncated gzip data"),
        (b"\x1f\x8b", "truncated gzip data"),
        (gzip_member(GZIP_REQUEST, crc=0), "trailer does not match"),
        (gzip_member(GZIP_REQUEST, size=len(GZIP_REQUEST) + 1), "trailer does not match"),
        (gzip_member(GZIP_REQUEST, method=7), "deflate method"),
        (gzip_member(GZIP_REQUEST, flags=0x08, optional=b"secret.txt\x00"), "flags"),
        (gzip_member(GZIP_REQUEST, flags=0x10, optional=b"test-secret\x00"), "flags"),
        (gzip_member(GZIP_REQUEST, flags=0x04, optional=b"\x04\x00test"), "flags"),
        (gzip_member(GZIP_REQUEST, flags=0x02, optional=b"\x00\x00"), "flags"),
        (gzip_member(GZIP_REQUEST, flags=0x01), "flags"),
        (gzip_member(GZIP_REQUEST, flags=0x20), "flags"),
        (gzip_member(GZIP_REQUEST, deflate=b"\xff\xff\xff\xff"), "invalid deflate data"),
        (gzip_member(gzip_member(GZIP_REQUEST)), "control character"),
        (gzip_member(b""), "empty request body"),
    ],
)
def test_malformed_gzip_is_rejected(body: bytes, match: str) -> None:
    rejects(check_gzip, body, match)
    rejects(check_v0, body, match)  # the same when the proxy hides Content-Encoding


def test_gzip_file_name_field_is_rejected() -> None:
    """Python's GzipFile writes FNAME when given a file name."""
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="test-secret.txt", mode="wb", fileobj=buffer, mtime=0) as out:
        out.write(GZIP_REQUEST)
    rejects(check_gzip, buffer.getvalue(), "flags")


def padded_deflate(data: bytes, empty_blocks: int) -> bytes:
    """Deflate ``data`` with extra empty stored blocks, which inflate to nothing."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    deflate = compressor.compress(data) + compressor.flush(zlib.Z_SYNC_FLUSH)
    # After a sync flush the stream is byte-aligned, and 00 00 00 ff ff is one empty,
    # non-final stored block (BTYPE 00, LEN 0, NLEN 0xffff).
    return deflate + b"\x00\x00\x00\xff\xff" * empty_blocks + compressor.flush()


def test_padded_deflate_encoding_is_rejected() -> None:
    """Empty deflate blocks do not change the request but could carry data to the server."""
    for blocks in (0, 1, 2):
        check_gzip(gzip_member(GZIP_REQUEST, deflate=padded_deflate(GZIP_REQUEST, blocks)))
    padded = gzip_member(GZIP_REQUEST, deflate=padded_deflate(GZIP_REQUEST, 1000))
    assert zlib.decompress(padded[10:-8], -zlib.MAX_WBITS) == GZIP_REQUEST
    rejects(check_gzip, padded, "larger than the request")


def test_stored_deflate_encoding_is_accepted() -> None:
    """An uncompressed (stored) deflate stream never exceeds the size bound."""
    request = v0_request(haves=(OID, OID2) * 2000, end=FLUSH)
    compressor = zlib.compressobj(0, zlib.DEFLATED, -zlib.MAX_WBITS)
    deflate = compressor.compress(request) + compressor.flush()
    assert len(deflate) > len(request)
    check_gzip(gzip_member(request, deflate=deflate))


def test_decompression_bomb_is_rejected_within_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    bomb = gzip_member(b"0" * (MAX_DECODED_BYTES * 4))
    assert len(bomb) < 64 * 1024
    produced: list[int] = []
    real = zlib.decompressobj

    class Spy:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._inflater = real(*args, **kwargs)

        def decompress(self, data: bytes, max_length: int = 0) -> bytes:
            assert 0 < max_length <= MAX_DECODED_BYTES + 1
            out = self._inflater.decompress(data, max_length)
            produced.append(len(out))
            return out

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inflater, name)

    monkeypatch.setattr(gitproto.zlib, "decompressobj", Spy)
    rejects(check_gzip, bomb, "decompresses to more than")
    assert produced == [MAX_DECODED_BYTES + 1]


def test_decoded_size_limit_is_exact() -> None:
    body = v0_request(haves=(OID,) * 30, end=FLUSH)
    member = gzip_member(body)
    check_v0(body, max_decoded_bytes=len(body))
    check_gzip(member, max_decoded_bytes=len(body))
    rejects(lambda b: check_v0(b, max_decoded_bytes=len(body) - 1), body, "larger than")
    rejects(lambda b: check_gzip(b, max_decoded_bytes=len(body) - 1), member, "more than")
    rejects(lambda b: check_gzip(b, max_decoded_bytes=0), member, "more than")


def test_plain_body_over_the_default_limit_is_rejected() -> None:
    have = pkt(b"have " + OID + b"\n")
    body = v0_request(end=b"") + have * (MAX_DECODED_BYTES // len(have)) + FLUSH
    assert len(body) > MAX_DECODED_BYTES
    rejects(check_v0, body, "larger than")


def test_negative_limit_is_a_caller_error() -> None:
    with pytest.raises(ValueError, match="max_decoded_bytes") as raised:
        check_v0(v0_request(), max_decoded_bytes=-1)
    assert not isinstance(raised.value, GitRequestError)
