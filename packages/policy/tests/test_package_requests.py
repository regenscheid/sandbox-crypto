# SPDX-License-Identifier: MIT
"""Classification of package cache requests (docs/PACKAGE-CACHE.md, "URL layout")."""

import pytest

from sandbox_science.policy import ClassificationError, PackageOperation, classify_package_request

SHA256 = "a" * 64
SHA512 = "b" * 128
MD5 = "c" * 32
PYPI_FILE = "packages/3f/6d/" + "c" * 60
WHEEL = f"{PYPI_FILE}/numpy-2.1.0-cp312-cp312-manylinux_2_17_x86_64.whl"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # PyPI
        ("/pypi/simple/numpy/", PackageOperation("pypi", "numpy", kind="index")),
        ("/pypi/simple/Zope.Interface/", PackageOperation("pypi", "zope-interface", kind="index")),
        (f"/pypi/files/numpy/{SHA256}/{WHEEL}", PackageOperation("pypi", "numpy")),
        (f"/pypi/files/numpy/-/{WHEEL}", PackageOperation("pypi", "numpy")),
        (
            f"/pypi/files/numpy/{SHA256}/{WHEEL}.metadata",
            PackageOperation("pypi", "numpy", kind="index"),
        ),
        # conda
        (
            "/conda/conda-forge/channeldata.json",
            PackageOperation("conda", None, "conda-forge", "index"),
        ),
        *[
            (
                f"/conda/conda-forge/linux-64/{name}",
                PackageOperation("conda", None, "conda-forge", "index"),
            )
            for name in (
                "repodata.json",
                "repodata.json.zst",
                "repodata.json.bz2",
                "current_repodata.json",
                "repodata_shards.msgpack.zst",
                f"shards/{SHA256}.msgpack.zst",
            )
        ],
        (
            "/conda/conda-forge/linux-64/libgcc-ng-13.2.0-h77fa898_7.conda",
            PackageOperation("conda", "libgcc-ng", "conda-forge"),
        ),
        (
            "/conda/conda-forge/noarch/python_abi-3.12-5_cp312.tar.bz2",
            PackageOperation("conda", "python_abi", "conda-forge"),
        ),
        # Cargo
        ("/cargo/index/config.json", PackageOperation("cargo", None, kind="index")),
        ("/cargo/index/se/rd/serde", PackageOperation("cargo", "serde", kind="index")),
        ("/cargo/index/1/a", PackageOperation("cargo", "a", kind="index")),
        ("/cargo/index/3/s/syn", PackageOperation("cargo", "syn", kind="index")),
        (f"/cargo/dl/serde/1.0.200/{SHA256}", PackageOperation("cargo", "serde")),
        (
            f"/cargo/dl/Serde_JSON/1.0.0-rc.1+build/{SHA256}",
            PackageOperation("cargo", "serde-json"),
        ),
        # Go
        (
            "/go/github.com/!azure/azure-sdk-for-go/@v/list",
            PackageOperation("go", "github.com/Azure/azure-sdk-for-go", kind="index"),
        ),
        ("/go/golang.org/x/mod/@latest", PackageOperation("go", "golang.org/x/mod", kind="index")),
        (
            "/go/golang.org/x/mod/@v/v0.20.0.info",
            PackageOperation("go", "golang.org/x/mod", kind="index"),
        ),
        (
            "/go/golang.org/x/mod/@v/v0.20.0.mod",
            PackageOperation("go", "golang.org/x/mod", kind="index"),
        ),
        ("/go/golang.org/x/mod/@v/v0.20.0.zip", PackageOperation("go", "golang.org/x/mod")),
        (
            "/go/github.com/x/y/@v/v2.0.0+incompatible.zip",
            PackageOperation("go", "github.com/x/y"),
        ),
        (
            "/go/example.com/m/@v/v1.0.0-!r!c1.zip",
            PackageOperation("go", "example.com/m"),
        ),
        (
            "/go/golang.org/toolchain/@v/v0.0.1-go1.23.0.linux-amd64.zip",
            PackageOperation("go", "golang.org/toolchain"),
        ),
        *[
            (f"/go/sumdb/sum.golang.org/{rest}", PackageOperation("go", None, kind="index"))
            for rest in ("supported", "latest", "tile/8/0/x123/456", "tile/8/1/000.p/12")
        ],
        # A checksum lookup names its module.
        (
            "/go/sumdb/sum.golang.org/lookup/github.com/!burnt!sushi/toml@v1.4.0",
            PackageOperation("go", "github.com/BurntSushi/toml", kind="index"),
        ),
        # conda-forge's shards sit beside repodata.
        (
            f"/conda/conda-forge/linux-64/{SHA256}.msgpack.zst",
            PackageOperation("conda", None, "conda-forge", "index"),
        ),
        # PyPI epochs and sdists
        (
            f"/pypi/files/numpy/-/{PYPI_FILE}/numpy-1!2.0.0.tar.gz",
            PackageOperation("pypi", "numpy"),
        ),
        (
            f"/pypi/files/zope-interface/-/{PYPI_FILE}/zope.interface-6.0.tar.gz",
            PackageOperation("pypi", "zope-interface"),
        ),
        # npm lockfile paths, legacy names and "@" separators
        ("/npm/lodash/-/lodash-4.17.21.tgz", PackageOperation("npm", "lodash")),
        ("/npm/@types/node/-/node-20.0.0.tgz", PackageOperation("npm", "@types/node")),
        ("/npm/@types%2fnode/-/node-20.0.0.tgz", PackageOperation("npm", "@types/node")),
        ("/npm/hello!world(1)", PackageOperation("npm", "hello!world(1)", kind="index")),
        (
            "/npm/files/-/thirty-two/-/thirty-two@0.0.1.tgz",
            PackageOperation("npm", "thirty-two"),
        ),
        ("/npm/files/-/express/-/express-1.0.0rc2.tgz", PackageOperation("npm", "express")),
        # npm
        ("/npm/lodash", PackageOperation("npm", "lodash", kind="index")),
        ("/npm/JSONStream", PackageOperation("npm", "JSONStream", kind="index")),
        ("/npm/files", PackageOperation("npm", "files", kind="index")),
        ("/npm/@types%2Fnode", PackageOperation("npm", "@types/node", kind="index")),
        ("/npm/@types%2fnode", PackageOperation("npm", "@types/node", kind="index")),
        ("/npm/@types/node", PackageOperation("npm", "@types/node", kind="index")),
        (
            f"/npm/files/{SHA512}/lodash/-/lodash-4.17.21.tgz",
            PackageOperation("npm", "lodash"),
        ),
        ("/npm/files/-/@types/node/-/node-20.0.0.tgz", PackageOperation("npm", "@types/node")),
        # opam: the repository index, and archives by digest (the path names no package)
        ("/opam/index.tar.gz", PackageOperation("opam", None, kind="index")),
        (f"/opam/cache/md5/cc/{MD5}", PackageOperation("opam", None, kind="artifact")),
        (f"/opam/cache/sha256/aa/{SHA256}", PackageOperation("opam", None, kind="artifact")),
        (f"/opam/cache/sha512/bb/{SHA512}", PackageOperation("opam", None, kind="artifact")),
        # Hackage: signed metadata, the index and source distributions
        *[
            (f"/hackage/{name}", PackageOperation("hackage", None, kind="index"))
            for name in (
                "root.json",
                "timestamp.json",
                "snapshot.json",
                "mirrors.json",
                "01-index.tar.gz",
            )
        ],
        ("/hackage/package/split-0.2.5.tar.gz", PackageOperation("hackage", "split")),
        (
            "/hackage/package/split-0.2.5/split-0.2.5.tar.gz",
            PackageOperation("hackage", "split"),
        ),
        ("/hackage/package/cryptol-3.6.0.tar.gz", PackageOperation("hackage", "cryptol")),
        (
            "/hackage/package/x86-64bit-0.4.6.2.tar.gz",
            PackageOperation("hackage", "x86-64bit"),
        ),
        ("/hackage/package/4Blocks-0.2.tar.gz", PackageOperation("hackage", "4Blocks")),
        (
            "/hackage/package/cctools-workqueue-3.6.1.0.1.0.0.1.tar.gz",
            PackageOperation("hackage", "cctools-workqueue"),
        ),
    ],
)
def test_cache_paths_are_classified(path: str, expected: PackageOperation) -> None:
    assert classify_package_request("GET", path) == expected
    assert classify_package_request("HEAD", path) == expected


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "get"])
def test_only_get_and_head_reach_the_cache(method: str) -> None:
    with pytest.raises(ClassificationError):
        classify_package_request(method, "/npm/lodash")


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/",
        "/maven/org/x",
        "pypi/simple/numpy/",
        # PyPI
        "/pypi/simple/",
        "/pypi/simple/numpy",
        "/pypi/simple/numpy/extra/",
        "/pypi/simple/num%70y/",
        "/pypi/simple/-numpy/",
        f"/pypi/files/NumPy/{SHA256}/{WHEEL}",
        f"/pypi/files/numpy/{SHA256[:-1]}/{WHEEL}",
        f"/pypi/files/numpy/{SHA256.upper()}/{WHEEL}",
        f"/pypi/files/numpy/{SHA256}/other/{WHEEL}",
        f"/pypi/files/numpy/{SHA256}/packages/../../{WHEEL}",
        f"/pypi/files/numpy/{SHA256}/packages//x.whl",
        "/pypi/json/numpy",
        # conda
        "/conda/conda-forge/linux-64/evil.json",
        "/conda/conda-forge/linux-64/repodata.json.gz",
        "/conda/Conda-Forge/linux-64/repodata.json",
        "/conda/conda-forge/linux-64/pkg.zip",
        "/conda/conda-forge/linux-64/nodashes.conda",
        f"/conda/conda-forge/linux-64/shards/{SHA256[:-1]}.msgpack.zst",
        "/conda/conda-forge/linux-64/../../etc/passwd",
        "/conda/conda-forge/linux-64/sub/dir/x-1-0.conda",
        # Cargo
        "/cargo/index/config.json.bak",
        "/cargo/api/v1/crates",
        "/cargo/api/v1/crates/new",
        f"/cargo/dl/serde/1.0.200/{SHA256[:-1]}",
        f"/cargo/dl/1serde/1.0.200/{SHA256}",
        "/cargo/index/../config.json",
        "/cargo/index/se/rd/Serde",
        # Go
        "/go/github.com/Azure/x/@v/list",
        "/go/example/x/@v/list",
        "/go/github.com/x/y/@v/v1.0.0.tar",
        "/go/github.com/x/y/@v/1.0.0.zip",
        "/go/github.com/x/y/@v/v1.0.0-RC1.zip",
        "/go/github.com/x/y/@v/v1.0.0.zip/extra",
        "/go/sumdb/other.org/latest",
        "/go/sumdb/sum.golang.org/../x",
        "/go/!/x/@v/list",
        "/go/github.com/!9/x/@v/list",
        "/go/.example.com/x/@v/list",
        "/go/golang.org%2Fx/mod/@v/list",
        # npm
        "/npm/-/v1/search",
        "/npm/-/npm/v1/security/advisories/bulk",
        "/npm/-/whoami",
        "/npm/_hidden",
        "/npm/.hidden",
        "/npm/@types",
        "/npm/@types%2Fnode/extra",
        "/npm/@types%2Fnode%2Fextra",
        "/npm/lodash%00",
        "/npm/lo%64ash",
        "/npm/lodash/latest",
        "/npm/files/zz/lodash/-/lodash-4.17.21.tgz",
        f"/npm/files/{SHA512}/lodash/-/lodash-4.17.21.tar",
        f"/npm/files/{SHA512}/lodash/-/../../x.tgz",
        # a file of another package under this package's name
        f"/pypi/files/numpy/-/{PYPI_FILE}/evil-1.0-py3-none-any.whl",
        f"/pypi/files/foo/-/{PYPI_FILE}/foo-bar-1.0.tar.gz",
        f"/pypi/files/numpy/-/{PYPI_FILE}/numpy-2.0.exe",
        "/npm/files/-/lodash/-/evil-1.0.0.tgz",
        "/npm/files/-/foo/-/foo-bar-1.0.0.tgz",
        "/npm/lodash/-/evil-1.0.0.tgz",
        "/npm/-/lodash-1.0.0.tgz",
        "/go/sumdb/sum.golang.org/lookup/github.com/Azure/x@v1.0.0",
        # opam: other repository files, opam 1's layout and malformed archive paths
        "/opam/repo",
        "/opam/version",
        "/opam/urls.txt",
        "/opam/index.tar",
        "/opam/packages/ocamlfind/ocamlfind.1.9.8/opam",
        "/opam/archives/ocamlfind.1.9.8+opam.tar.gz",
        f"/opam/cache/sha256/{SHA256}",
        f"/opam/cache/sha256/ab/{SHA256}",  # not the digest's first two digits
        f"/opam/cache/sha256/aa/{SHA256[:-1]}",
        f"/opam/cache/sha256/aa/{SHA512}",
        f"/opam/cache/md5/aa/{SHA256}",
        f"/opam/cache/sha256/AA/{SHA256.upper()}",
        f"/opam/cache/sha1/aa/{'a' * 40}",
        f"/opam/cache/sha256/aa/{SHA256}/",
        f"/opam/cache/sha256/aa/{SHA256}/x",
        f"/opam/cache/sha256/aa/../aa/{SHA256}",
        f"/opam/cache/sha256/aa%2F{SHA256}",
        # Hackage: other index forms, web pages and API, and files of another package
        "/hackage/01-index.tar",
        "/hackage/00-index.tar.gz",
        "/hackage/targets.json",
        "/hackage/package/split",
        "/hackage/package/split-0.2.5",
        "/hackage/package/split-0.2.5/split.cabal",
        "/hackage/package/split-0.2.5/revision/1.cabal",
        "/hackage/package/split-0.2.5/split-0.2.6.tar.gz",
        "/hackage/package/split-0.2.5/evil-0.2.5.tar.gz",
        "/hackage/package/split/split-0.2.5.tar.gz",
        "/hackage/package/split-01.tar.gz",
        "/hackage/package/split-0.2.5a.tar.gz",
        "/hackage/package/1-0.2.5.tar.gz",
        "/hackage/package/-split-0.2.5.tar.gz",
        "/hackage/package/split--x-0.2.5.tar.gz",
        "/hackage/package/split_x-0.2.5.tar.gz",
        "/hackage/package/split-0.2.5.tgz",
        "/hackage/package/split%2D0.2.5.tar.gz",
        "/hackage/packages/archive/split/0.2.5/split-0.2.5.tar.gz",
        "/hackage/api/v1/packages",
        # characters outside the URL-safe set, which OpenShell would have escaped
        "/npm/lodash?x=1",
        "/npm/lodash#x",
        "/npm/lo dash",
        "/npm/lodash\\x",
        "/npm/lödash",
    ],
)
def test_other_paths_are_refused(path: str) -> None:
    with pytest.raises(ClassificationError):
        classify_package_request("GET", path)


def test_names_are_normalized_where_the_registry_folds_spellings() -> None:
    assert PackageOperation("pypi", "Zope_Interface").name == "zope-interface"
    assert PackageOperation("cargo", "Serde_JSON").name == "serde-json"
    assert PackageOperation("npm", "JSONStream").name == "JSONStream"
    assert PackageOperation("go", "github.com/Azure/x").name == "github.com/Azure/x"
    assert PackageOperation("hackage", "HTTP").name == "HTTP"
    assert PackageOperation("opam", "ocaml-base-compiler").name == "ocaml-base-compiler"


def test_opam_archives_are_the_only_package_files_without_a_name() -> None:
    # opam fetches archives by digest, so the path names no package; the policy
    # decides them by ecosystem.
    assert PackageOperation("opam", None, kind="artifact").name is None
    for ecosystem in ("pypi", "cargo", "go", "npm", "hackage"):
        with pytest.raises(ClassificationError, match="named package"):
            PackageOperation(ecosystem, None, kind="artifact")  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("ecosystem", "name", "channel", "kind"),
    [
        ("maven", "x", None, "artifact"),
        ("pypi", "x", None, "download"),
        ("pypi", None, None, "artifact"),
        ("pypi", "x", "conda-forge", "artifact"),
        ("conda", "x", None, "artifact"),
        ("npm", "a" * 215, None, "index"),
        ("go", "example/x", None, "index"),
        ("go", "example.com/../x", None, "index"),
        ("cargo", "1x", None, "index"),
        # Only opam's content-addressed archives have no name.
        ("hackage", None, None, "artifact"),
        ("hackage", "1", None, "artifact"),
        ("hackage", "split-", None, "artifact"),
        ("hackage", "split_x", None, "artifact"),
        ("opam", "1", None, "index"),
        ("opam", "ocaml.findlib", None, "index"),
        ("opam", None, "conda-forge", "artifact"),
    ],
)
def test_invalid_operations_are_rejected(
    ecosystem: str, name: str | None, channel: str | None, kind: str
) -> None:
    with pytest.raises(ClassificationError):
        PackageOperation(ecosystem, name, channel, kind)  # pyright: ignore[reportArgumentType]
