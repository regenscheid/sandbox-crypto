# Package cache

Research sandboxes install packages only through the package cache (D44, D71). It is a pull-through cache, not a mirror. Nothing is stored until a sandbox asks for it. Package files are kept until the cache reaches its size cap, and then the least recently used are evicted. Index data is re-checked after a short time. The code is in `packages/package-cache` (`sandbox_science.package_cache`).

The first version covers five ecosystems: PyPI, conda (conda-forge), Rust crates, Go modules and npm. opam and Hackage are planned (EasyCrypt, Jasmin, ProVerif, Cryptol, Tamarin). C and C++ libraries and the toolchains for every language come from conda-forge. Sources that a build fetches itself (git submodules, release tarballs) go through the policy middleware like any other download.

## How a request flows

1. A sandbox's package client is configured to use the cache (see [Client configuration](#client-configuration)). It requests, for example, `http://<cache>/pypi/simple/numpy/`.
2. OpenShell admits the request (the cache is an endpoint in the compiled policy) and calls the policy middleware. The middleware knows which sandbox is asking. It classifies the request with the policy library as a package operation (ecosystem, index or artifact, package name, conda channel) and decides it with the project policy's `packages` section.
3. The cache serves the request from its store, or fetches it from the upstream registry, verifies it, stores it and serves it.

The cache itself makes no policy decisions and does not know which sandbox is asking. It only ever reaches the configured upstream registries.

What the policy adds for packages ([policy](POLICY.md), "Packages"):

- **Pool ceiling.** The ceiling applies to the registries each ecosystem uses, so a pool that forbids those hosts gets no packages.
- **Go module origins.** When the Go module proxy lacks a module, it fetches it from the module's origin, so a lookup for a made-up module would send data to whatever host the module path names. A Go module request, including a checksum-database lookup, is therefore also decided as a read of its origin host under the network rules. Under the permissive default, every public origin is readable anyway.
- **Registries only through the cache.** The registries and their aliases and mirrors are on the floor for ordinary HTTP. Installs straight from version control (`pip install git+https://…`, Cargo and npm git dependencies, Go without a proxy) are ordinary git and HTTP requests: the network rules decide them, not `packages`.

## URL layout

The policy library (`classify_package_request`) and the cache's adapters share this layout. Anything else is refused. Paths are matched as received, still percent-encoded where noted.

| Path | Kind | Upstream |
| --- | --- | --- |
| `/pypi/simple/<project>/` | index, per package | `https://pypi.org/simple/<project>/` |
| `/pypi/files/<project>/<sha256>/<path>` | artifact | `https://files.pythonhosted.org/<path>` |
| `/pypi/files/<project>/<sha256>/<path>.metadata` | index, per package (PEP 658, immutable) | `https://files.pythonhosted.org/<path>.metadata` |
| `/conda/<channel>/channeldata.json` | index, whole channel | `https://conda.anaconda.org/<channel>/channeldata.json` |
| `/conda/<channel>/<subdir>/<index file>` | index, whole channel | `repodata.json`, `current_repodata.json`, `repodata_shards.msgpack.zst`, each also as `.zst`/`.bz2` where conda publishes it |
| `/conda/<channel>/<subdir>/<sha256>.msgpack.zst` | index, whole channel (a shard: content-addressed, immutable) | same path upstream (the shard index's base URL is empty, so shards sit beside repodata; a `shards/` directory is also accepted) |
| `/conda/<channel>/<subdir>/<name>-<version>-<build>.conda` or `.tar.bz2` | artifact | same path upstream |
| `/cargo/index/config.json` | index, whole registry (built by the cache) | none |
| `/cargo/index/<prefix>/<crate>` | index, per package | `https://index.crates.io/<prefix>/<crate>` |
| `/cargo/dl/<crate>/<version>/<sha256>` | artifact | `https://static.crates.io/crates/<crate>/<crate>-<version>.crate` |
| `/go/<module>/@v/list`, `/go/<module>/@latest` | index, per package | `https://proxy.golang.org/<module>/...` |
| `/go/<module>/@v/<version>.info`, `.mod` | index, per package (immutable) | same path upstream |
| `/go/<module>/@v/<version>.zip` | artifact | same path upstream |
| `/go/sumdb/sum.golang.org/supported` | index, whole registry (built by the cache) | none |
| `/go/sumdb/sum.golang.org/latest`, `/go/sumdb/sum.golang.org/tile/...` | index, whole registry | `https://sum.golang.org/<path>` |
| `/go/sumdb/sum.golang.org/lookup/<module>@<version>` | index, per package | `https://sum.golang.org/lookup/...` |
| `/npm/<name>`, `/npm/@<scope>%2F<name>`, `/npm/@<scope>/<name>` | index, per package | `https://registry.npmjs.org/<name>` |
| `/npm/files/<sha512>/<name>/-/<file>.tgz` | artifact | `https://registry.npmjs.org/<name>/-/<file>.tgz` |
| `/npm/<name>/-/<file>.tgz` | artifact, no digest | the same; the path npm, pnpm and yarn build from a lockfile, which they check against its integrity |

Notes:
- **Digests in paths.** `<sha256>` and `<sha512>` are lowercase hex digests that the cache copied from the index when it rewrote the download links. They name the bytes the cache must verify, or `-` when the index published none. Cargo puts the checksum in the path itself, through the `{sha256-checksum}` marker of its `dl` template. A PyPI `.metadata` path carries the distribution's digest, not the metadata file's; the client checks the metadata against the hash on the index page.
- **Files belong to the package in the path.** A PyPI file must be a distribution of `<project>` and an npm tarball one of `<name>`; both the cache and the policy check it, so a decision about one package cannot fetch another.
- **Module paths.** `<module>` is a Go module path in the proxy's case encoding: an uppercase letter is written `!` followed by the lowercase letter.
- **npm names.** `<name>` is an npm package name. A scoped name keeps its `@scope/` part.
- **No rewriting for conda.** conda-forge's index files use paths relative to the channel, so the cache passes them through unchanged.
- **Cargo.** The cache builds `/cargo/index/config.json` itself: `dl` points back at the cache, and there is no `api`, so publishing and logins are impossible.
- **Go checksum database.** The cache answers `supported` itself, so the `go` command fetches the checksum database through the cache. The `go` command verifies every module against it.
- **What clients cannot reach.** Only GET and HEAD are accepted. Search, audit (npm's audit POST), publishing, logins and the PyPI root index (`/pypi/simple/`) are refused.

## Storage and eviction

- **Content-addressed files.** Bodies are stored once, named by their SHA-256 (`blobs/ab/abcd…`), so identical files are shared. An SQLite index maps each cache key (upstream resource and response variant) to its blob, size, kind, fetch time, last access time and validators (`ETag`, `Last-Modified`).
- **Artifacts and immutable index files** are kept until evicted and never revalidated.
- **Other index data** is fresh for `metadata_ttl` (default 5 minutes). After that, the cache revalidates it with the stored validators. If upstream fails, the stale copy is served for up to `stale_if_error` (default 1 day).
- **Size cap.** When the store exceeds `max_size` (default 100 GiB), the least recently accessed entries are removed until it is under 90% of the cap. A blob is deleted when no entry refers to it. An evicted file is simply fetched again the next time it is needed.
- **Concurrent requests.** Concurrent requests for the same key share one upstream fetch.

## Verification

- **Artifacts with a digest in the path.** The cache verifies the download against that digest before it stores or serves it: PyPI (SHA-256 from the index), Rust (the index's checksum) and npm (the SHA-512 `integrity`). A mismatch is an error (502), and nothing is stored.
- **conda packages and Go modules** are verified by the clients: conda against repodata's SHA-256, and the `go` command against the checksum database. The cache stores them as received over TLS from the configured upstream.
- **Size limits.** `max_artifact_size` and `max_metadata_size` bound every download.

## Logging and limits

- **Logs.** Every request is logged with ecosystem, path, cache result (hit, miss, revalidated, stale), status, size and upstream time. Package names can carry data out (a lookup for a made-up name still reaches the registry), and the policy middleware's and OpenShell's logs tie each request to its sandbox.
- **Rate limit.** Upstream fetches are rate-limited (a token bucket shared by all clients), so a flood of lookups for made-up names cannot hammer a registry through the cache.
- **Rate limit details.** Every upstream request, each redirect hop included, takes a token. A request waits up to 10 seconds for one, then gets 503 with `Retry-After: 1`, unless a stale copy can be served.
- **Status.** `GET /-/health` and `GET /-/stats` (entries, bytes, hits, misses, revalidations, stale answers, errors) are served on the same listener. The middleware refuses both for sandboxes, since they are not in the URL layout.
- **Sharing.** The store is shared by every project on the host, so timing (a hit or a miss) shows whether some sandbox fetched a file before. Treat package names as visible across projects.

The store lives in `cache_dir`: `blobs/`, the SQLite index `index.sqlite3`, `tmp/` for downloads in progress, and a `lock` that keeps a second server from using the same directory. `sandbox-science-package-cache stats` and `prune` (evict to the cap now) can run beside the server.

## Settings

```toml
listen = "127.0.0.1:8090"
public_url = "http://host.openshell.internal:8090"   # how sandboxes reach the cache; rewritten links use it
cache_dir = "cache"
max_size = "100Gi"
metadata_ttl = "5m"
stale_if_error = "1d"
max_artifact_size = "4Gi"
max_metadata_size = "1Gi"
upstream_rate = 20           # upstream fetches per second
upstream_burst = 200

[pypi]
index = "https://pypi.org/simple"
files = "https://files.pythonhosted.org"

[conda]
base = "https://conda.anaconda.org"
channels = ["conda-forge"]
subdirs = ["linux-64", "linux-aarch64", "noarch"]

[cargo]
index = "https://index.crates.io"
download = "https://static.crates.io/crates"

[go]
proxy = "https://proxy.golang.org"
sumdb = "https://sum.golang.org"

[npm]
registry = "https://registry.npmjs.org"
```

An ecosystem whose table is absent is disabled. Its paths return 404.

## Client configuration

The environment layer writes these into each sandbox, where `<cache>` is the cache's `public_url`:

| Client | Setting |
| --- | --- |
| pip, uv | `index-url = <cache>/pypi/simple/` (and `trusted-host` while the cache is plain HTTP) |
| conda, mamba, pixi | `channel_alias: <cache>/conda`, `channels: [conda-forge]` |
| cargo | `[source.crates-io] replace-with = "cache"`, `[source.cache] registry = "sparse+<cache>/cargo/index/"` |
| go | `GOPROXY=<cache>/go`, `GOSUMDB=sum.golang.org` (checksum lookups go through the proxy), `GOTOOLCHAIN=local` |
| npm, pnpm, yarn 1 | `registry=<cache>/npm/`, `audit=false`, `fund=false` in `.npmrc` |
| yarn 2+ | `npmRegistryServer: <cache>/npm` and `unsafeHttpWhitelist` for the cache's host in `.yarnrc.yml` (yarn ignores `.npmrc`) |

## Running it

```bash
uv run sandbox-science-package-cache serve --settings cache.toml
```

- **On babbage (VM driver).** Each sandbox's supervisor runs on the host, so the cache listens on loopback (`listen = "127.0.0.1:8090"`). Sandboxes reach it as `host.openshell.internal:8090`, which is also its `public_url`. Name it that way in the OpenShell targets (`package_cache: {host: host.openshell.internal, port: 8090}`). The traffic never leaves the host, so plain HTTP is acceptable there.
- **Inspecting and pruning.** `stats` prints the counters and store size as JSON. `prune` evicts down to the cap now. Both run beside a serving cache.
- **Real clients.** `SS_INTEGRATION=1 uv run pytest packages/package-cache/tests/test_clients_integration.py` installs a small package with pip, uv, mamba, cargo, go and npm through a cache it starts on loopback. It needs access to the public registries.

## Adapters

Each ecosystem is an adapter (`sandbox_science.package_cache.adapter`). Given the request path below its prefix, it returns one of three answers:
- a `Fetch`: the upstream URL, cache key, kind, whether the result is immutable, the request headers to send, the expected digest, and a rewrite function for index data;
- a `Static` response it builds itself;
- `None`, a 404, for anything it does not recognize.

Adapters are pure functions of the path, the request headers and their settings, so they are tested without a network. The core does all I/O.

## Planned

- opam and Hackage adapters.
- A cooling-off period: refuse versions published in the last N days unless approved. Every one of the five ecosystems publishes release times. The middleware can pass the project's setting to the cache in a request header that it sets itself, overwriting anything the sandbox sent; the cache must accept the setting only from the middleware.
- conda index files are passed through unchanged because conda-forge's base URLs are relative (empty). The cache should refuse index files whose `base_url` is absolute, in case a channel changes that.
- Keeping files that active lockfiles use from eviction.
- TLS between OpenShell and the cache, for deployments where the cache is not on the gateway's host.
