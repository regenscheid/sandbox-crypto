# SPDX-License-Identifier: MIT
"""Asking the package cache which opam packages an archive belongs to."""

import http.server
import json
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from sandbox_science.policy_middleware.archives import CacheArchiveLookup

DIGEST = "md5:ca770e5806032a96131b670f6e07f146"
PATH = "/-/opam/archives/md5/ca770e5806032a96131b670f6e07f146"


@dataclass
class Answer:
    """What the stand-in cache answers, and the paths it was asked for."""

    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict[str, str])
    delay: float = 0.0
    paths: list[str] = field(default_factory=list[str])


def answer_json(packages: object, digest: str = DIGEST) -> bytes:
    return json.dumps({"digest": digest, "packages": packages}).encode()


@pytest.fixture
def cache() -> Iterator[tuple[str, Answer]]:
    """A stand-in for the package cache's archive route on 127.0.0.1."""
    answer = Answer()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            answer.paths.append(self.path)
            time.sleep(answer.delay)
            try:
                self.send_response(answer.status)
                for name, value in answer.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(answer.body)))
                self.end_headers()
                self.wfile.write(answer.body)
            except ConnectionError:
                pass  # the lookup gave up waiting

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", answer
    finally:
        server.shutdown()
        server.server_close()


def test_the_packages_come_from_the_caches_route(cache: tuple[str, Answer]) -> None:
    url, answer = cache
    answer.body = answer_json(["ocamlfind.1.9.8", "findlib.1.9.8"])
    assert CacheArchiveLookup(url).packages(DIGEST) == ("findlib.1.9.8", "ocamlfind.1.9.8")
    assert CacheArchiveLookup(url + "/").packages(DIGEST) == ("findlib.1.9.8", "ocamlfind.1.9.8")
    assert answer.paths == [PATH, PATH]
    answer.body = answer_json([])
    assert CacheArchiveLookup(url).packages(DIGEST) == ()


def test_proxies_from_the_environment_are_ignored(
    cache: tuple[str, Answer], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, answer = cache
    answer.body = answer_json(["ocamlfind.1.9.8"])
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    assert CacheArchiveLookup(url).packages(DIGEST) == ("ocamlfind.1.9.8",)


@pytest.mark.parametrize(
    ("status", "body", "headers"),
    [
        pytest.param(503, b"the opam index has not been read yet\n", {}, id="no-table-yet"),
        pytest.param(404, b"not found\n", {}, id="opam-not-enabled"),
        pytest.param(302, b"", {"Location": "http://example.org/"}, id="redirect"),
        pytest.param(200, b"not json", {}, id="not-json"),
        pytest.param(200, b"[]", {}, id="not-an-object"),
        pytest.param(200, answer_json(["ocamlfind.1.9.8"], "md5:" + "0" * 32), {}, id="other"),
        pytest.param(200, answer_json("ocamlfind.1.9.8"), {}, id="not-a-list"),
        pytest.param(200, answer_json(["ocamlfind"]), {}, id="no-version"),
        pytest.param(200, answer_json(["ocamlfind.1.9.8; rm -rf /"]), {}, id="not-an-id"),
        pytest.param(200, answer_json(["../x.1"]), {}, id="path"),
        pytest.param(200, answer_json([1]), {}, id="number"),
        pytest.param(200, answer_json(["a.1"] * 20_000), {}, id="too-large"),
    ],
)
def test_anything_but_a_well_formed_answer_names_nothing(
    cache: tuple[str, Answer], status: int, body: bytes, headers: dict[str, str]
) -> None:
    url, answer = cache
    answer.status, answer.body, answer.headers = status, body, headers
    assert CacheArchiveLookup(url).packages(DIGEST) is None


def test_a_slow_cache_names_nothing_and_does_not_hold_the_decision(
    cache: tuple[str, Answer],
) -> None:
    url, answer = cache
    answer.body = answer_json(["ocamlfind.1.9.8"])
    answer.delay = 0.5
    started = time.monotonic()
    assert CacheArchiveLookup(url, timeout=0.05).packages(DIGEST) is None
    assert time.monotonic() - started < 1.0


def test_an_unreachable_cache_names_nothing() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert CacheArchiveLookup(f"http://127.0.0.1:{port}").packages(DIGEST) is None


def test_a_cache_that_does_not_speak_http_names_nothing() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)

        def answer() -> None:
            connection, _ = listener.accept()
            with connection:
                connection.recv(4096)
                connection.sendall(b"garbage\r\n\r\n")

        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        assert CacheArchiveLookup(url).packages(DIGEST) is None
        thread.join(timeout=5)
