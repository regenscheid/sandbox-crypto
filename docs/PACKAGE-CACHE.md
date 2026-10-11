# Package cache

Research sandboxes install packages only through the package cache (D44, D71). It is a pull-through cache, not a mirror. Nothing is stored until a sandbox asks for it. Package files are kept until the cache reaches its size cap, and then the least recently used are evicted. Index data is re-checked after a short time. The code is in `packages/package-cache` (`sandbox_science.package_cache`).

It covers seven ecosystems: PyPI, conda (conda-forge), Rust crates, Go modules, npm, opam (opam.ocaml.org: EasyCrypt's dependencies, Jasmin, ProVerif) and Hackage (Cryptol, Tamarin's dependencies). C and C++ libraries and the toolchains for every language come from conda-forge (see [Toolchains](#toolchains)). Sources that a build fetches itself (git submodules, release tarballs) go through the policy middleware like any other download.

## How a request flows

1. A sandbox's package client is configured to use the cache (see [Client configuration](#client-configuration)). It requests, for example, `http://<cache>/pypi/simple/numpy/`.
2. OpenShell admits the request (the cache is an endpoint in the compiled policy) and calls the policy middleware. The middleware knows which sandbox is asking. It classifies the request with the policy library as a package operation (ecosystem, index or artifact, package name, conda channel) and decides it with the project policy's `packages` section.
3. The cache serves the request from its store, or fetches it from the upstream registry, verifies it, stores it and serves it.

The cache itself makes no policy decisions and does not know which sandbox is asking. It fetches only from the configured upstream registries. It follows their redirects only to public hosts, refusing loopback, private and internal names and addresses.

What the policy adds for packages ([policy](POLICY.md), "Packages"):

- **Pool ceiling.** The ceiling applies to the registries each ecosystem uses, so a pool that forbids those hosts gets no packages.
- **Go module origins.** When the Go module proxy lacks a module, it fetches it from the module's origin, so a lookup for a made-up module would send data to whatever host the module path names. A Go module request, including a checksum-database lookup, is therefore also decided as a read of its origin host under the network rules. Under the permissive default, every public origin is readable anyway.
- **Registries only through the cache.** The registries and their aliases and mirrors are on the floor for ordinary HTTP. Installs straight from version control (`pip install git+https://…`, Cargo and npm git dependencies, Go without a proxy, `opam pin` from a git URL, opam-repository used as a git repository) are ordinary git and HTTP requests: the network rules decide them, not `packages`.
- **opam's fallbacks.** opam takes an archive from opam.ocaml.org's archive cache through the cache first. When that fails for any reason, opam downloads it from the archive's own URL (often a GitHub release), the mirrors its opam file lists, or Software Heritage: ordinary HTTP, decided by the network rules. That includes a 404 (opam.ocaml.org lacks the archive) and a 403 from the policy middleware, which is how a `packages.opam` of `escalate` or `deny` reaches opam. opam checks those downloads against the opam file's checksums too.
  - **What `packages.opam` decides.** Only the archives that come through the cache. Under network rules that allow GETs from GitHub or public hosts, as the example policy's do, `escalate` and `deny` do not hold: opam installs from the archives' own URLs, leaving a pending escalation that no longer gates anything. To make them hold, the rules must deny or escalate those downloads as well. The OpenShell compiler warns when they do not ([policy](POLICY.md), "Packages").
  - **With `deny`.** opam then cannot read the index through the cache either, but an opam root that already has one (from a base image, or from opam-repository fetched with git) still installs from the archives' own URLs.
- **cabal's mirrors.** When the cache fails, cabal tries the mirrors in Hackage's signed `mirrors.json`. They are on the floor, so packages still come only through the cache.

## URL layout

The policy library (`classify_package_request`) and the cache's adapters share this layout. Anything else is refused. Paths are matched as received, still percent-encoded where noted.

| Path | Kind | Upstream |
| --- | --- | --- |
| `/pypi/simple/<project>/` | index, per package | `https://pypi.org/simple/<project>/` |
| `/pypi/files/<project>/<sha256>/<path>` | artifact | `https://files.pythonhosted.org/<path>` |
| `/pypi/files/<project>/<sha256>/<path>.metadata` | index, per package (PEP 658, immutable) | `https://files.pythonhosted.org/<path>.metadata` |
| `/conda/<channel>/channeldata.json` | index, whole channel | `https://conda.anaconda.org/<channel>/channeldata.json` |
| `/conda/<channel>/<subdir>/<index file>` | index, whole channel | `repodata.json`, `current_repodata.json`, `repodata_shards.msgpack.zst`, each also as `.zst`/`.bz2` where conda publishes it |
| `/conda/<channel>/<subdir>/<sha256>.msgpack.zst` | index, whole channel (a shard: content-addressed, immutable, verified against the SHA-256 in its name) | same path upstream (the shard index's base URL is empty, so shards sit beside repodata; a `shards/` directory is also accepted) |
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
| `/npm/<name>/-/<file>.tgz`, `/npm/@<scope>%2F<name>/-/<file>.tgz` | artifact, no digest | the same, the scope's slash literal; the path npm, pnpm and yarn build from a lockfile (yarn 2+ writes the scope's slash `%2f`), which they check against its integrity |
| `/opam/index.tar.gz` | index, whole repository | `https://opam.ocaml.org/index.tar.gz` |
| `/opam/cache/<algorithm>/<xx>/<digest>` | artifact (names no package) | the same path upstream: opam.ocaml.org's archive cache, by `md5`, `sha256` or `sha512`; `<xx>` is the digest's first two digits |
| `/hackage/root.json`, `timestamp.json`, `snapshot.json`, `mirrors.json` | index, whole registry (revalidated on every request) | `https://hackage.haskell.org/<file>` |
| `/hackage/01-index.tar.gz` | index, whole registry (revalidated on every request; answers byte ranges) | `https://hackage.haskell.org/01-index.tar.gz` |
| `/hackage/package/<name>-<version>.tar.gz`, `/hackage/package/<id>/<id>.tar.gz` | artifact, no digest | `https://hackage.haskell.org/package/<id>/<id>.tar.gz`; the first form is cabal's, the second Hackage's own (the same `<id>` twice), and both share one entry |

Notes:
- **Digests in paths.** `<sha256>` and `<sha512>` are lowercase hex digests that the cache copied from the index when it rewrote the download links. They name the bytes the cache must verify, or `-` when the index published none. Cargo puts the checksum in the path itself, through the `{sha256-checksum}` marker of its `dl` template. A PyPI `.metadata` path carries the distribution's digest, not the metadata file's; the client checks the metadata against the hash on the index page.
- **Files belong to the package in the path.** A PyPI file must be a distribution of `<project>`, an npm tarball one of `<name>`, and a Hackage source distribution the one its `<name>-<version>` names (in Hackage's form, the same id twice); both the cache and the policy check it, so a decision about one package cannot fetch another. opam's archives are the exception: opam fetches them by digest, so the path names no package, and the policy decides them by ecosystem alone. An escalation records the digest, and the packages the cache's copy of the index lists for it (see "opam" below).
- **Module paths.** `<module>` is a Go module path in the proxy's case encoding: an uppercase letter is written `!` followed by the lowercase letter.
- **npm names.** `<name>` is an npm package name. A scoped name keeps its `@scope/` part.
- **No rewriting for conda.** conda-forge's index files use paths relative to the channel, so the cache passes them through unchanged.
- **Cargo.** The cache builds `/cargo/index/config.json` itself: `dl` points back at the cache, and there is no `api`, so publishing and logins are impossible.
- **Go checksum database.** The cache answers `supported` itself, so the `go` command fetches the checksum database through the cache. The `go` command verifies every module against it.
- **opam.** opam 2 downloads only `index.tar.gz` from an HTTP repository, and reads the repository's `repo` and `version` files and every package's opam file from it; the cache serves no other repository file. The `repo` file of opam.ocaml.org says `archive-mirrors: "cache"`, so opam looks for each archive (and each extra source, such as a patch) in `<repository>/cache` first, by the first checksum its opam file lists. That is MD5 for about a quarter of opam-repository (the rest SHA-256, a few SHA-512), so all three are accepted. Nothing is rewritten.
  - **Which package an archive is.** From the index it holds, the cache keeps a table from every checksum the opam files list to the packages listing it. It builds the table when it stores a new index (under a second for opam.ocaml.org's, whose 19,000 opam files list 22,000 checksums), and answers `GET /-/opam/archives/<algorithm>/<hex>` from memory with `{"digest": "md5:<hex>", "packages": ["ocamlfind.1.9.8"]}`, or 503 until it has read an index. An empty list means that no opam file in the index lists the digest. Packages share archives (all 108 of one project's packages list the same one), so the list can be long. The policy middleware asks when it records an escalation or alert for an archive ([policy middleware](POLICY-MIDDLEWARE.md), "Results"). Sandboxes cannot use the route: the middleware refuses paths outside the layout.
- **Hackage.** cabal uses hackage-security (TUF): it verifies the signed metadata, the index and every package itself, so the cache passes all of them through unchanged. `timestamp.json` names the snapshot by hash, and the snapshot names the index, the root and the mirror list the same way, so a client must get them as they were at one moment. The cache therefore revalidates each of these files, and the index, on every request (a conditional request, answered 304 while the file is unchanged). With any lifetime, it could pair a new file with an older one that it names differently, and cabal cannot ask past a cache: hackage-security retries a failed check with `Cache-Control: max-age=0`, but cabal drops its `Cache-Control` headers ([bug description](../.agents/upstream/cabal-install-drops-cache-control-headers.md)). Hackage's own CDN caches each of these files (`Cache-Control: max-age=60`), so this keeps them as consistent as fetching them from Hackage directly, no more. The uncompressed `01-index.tar` (about 1 GiB, which hackage-security never fetches) and the unsigned `00-index.tar.gz` are refused.
- **What clients cannot reach.** Only GET and HEAD are accepted. Search, audit (npm's audit POST), publishing, logins, the PyPI root index (`/pypi/simple/`) and Hackage's web pages and API are refused.

### Byte ranges

cabal keeps a copy of Hackage's index (`01-index.tar.gz`, about 140 MB) and updates it with one range request for the part it lacks: the last 64 KiB of its copy up to the end of the new index (`Range: bytes=<from>-<to>`). The cache answers a GET that asks for one byte range of a stored body, of any ecosystem, with 206 Partial Content and `Content-Range`, cut from the stored copy (for the index, the copy it has just revalidated, or the stale copy it serves when upstream fails). A range that starts beyond the body gets 416. Everything else gets the whole body (200), as HTTP allows: several ranges, a malformed header, units other than bytes, a HEAD, and any request with `If-Range`, since the cache gives clients no validators that could name what it stores. The cache does not advertise ranges (`Accept-Ranges`); cabal sends them regardless. A client that got the whole body instead of a range still works: hackage-security then replaces its copy, so ignoring ranges would cost a full 140 MB transfer on every update. The policy middleware decides a range request by its path, like any other GET.

## Storage and eviction

- **Content-addressed files.** Bodies are stored once, named by their SHA-256 (`blobs/ab/abcd…`), so identical files are shared. An SQLite index maps each cache key (upstream resource and response variant) to its blob, size, kind, fetch time, last access time and validators (`ETag`, `Last-Modified`).
- **Artifacts and immutable index files** are kept until evicted and not revalidated, with one exception. Hackage's source distributions, which the cache cannot verify, are revalidated a day after the cache fetched or last confirmed them: a conditional request with Hackage's `ETag`. A 304 confirms the copy and a 200 replaces it. If upstream fails, answers 404 or sends something that is not a gzip file, the stored copy is served.
- **Other index data** is fresh for `metadata_ttl` (default 5 minutes). After that, the cache revalidates it with the stored validators. An adapter can shorten that lifetime for a file (its `max_age`): Hackage's signed metadata and index have none, so they are revalidated on every request. If upstream fails, the stale copy is served for up to `stale_if_error` (default 1 day). A stored artifact is served however old it is.
- **Size cap.** Toward `max_size` (default 100 GiB), each entry counts its blob (a shared blob once) plus 1 KiB, about what its row in the index takes. "Not found" answers share one small blob, so without that overhead they could grow the index without limit. When the store exceeds the cap, eviction first purges the negative entries that are of no further use (older than their TTL plus `stale_if_error`). Then it removes the least recently accessed entries until the store is under 90% of the cap. A blob is deleted when no entry refers to it. An evicted file is simply fetched again the next time it is needed.
- **Downloads in progress.** Bodies being downloaded are written to `tmp/`. Together they may hold at most `max_temp_size` (default 16 GiB, and never more than `max_size`). Each download reserves its `Content-Length` before it writes. Without one, or when index data decodes to more, it reserves its size as it arrives. A download that would not fit fails at once with 503 and `Retry-After: 1`. The disk therefore holds at most `max_size` plus `max_temp_size`, plus the rewritten copies of the at most two index documents being rewritten.
- **Concurrent requests.** Concurrent requests for the same key share one upstream fetch. Keys of digest-checked files include the expected digest, so a request naming a wrong digest never shares a fetch, or a stored file, with a correct one.

## Verification

- **Artifacts with a digest in the path.** The cache verifies the download against that digest before it stores or serves it: PyPI (SHA-256 from the index), Rust (the index's checksum), npm (the SHA-512 `integrity`) and opam (the checksum opam names the archive by: MD5, SHA-256 or SHA-512). A mismatch is an error (502), and nothing is stored. opam then checks every checksum its opam file lists, a stronger one than MD5 included (only two opam-repository packages list MD5 alone).
- **conda shards** are verified against the SHA-256 in their names.
- **conda packages and Go modules** are verified by the clients: conda against repodata's SHA-256, and the `go` command against the checksum database. The cache stores them as received over TLS from the configured upstream.
- **Hackage** is verified by cabal: hackage-security checks the signed metadata against Hackage's root keys, the index against the snapshot, and every source distribution against the length and SHA-256 that the index lists for it (in its `package.json` targets). The cache serves the metadata unchanged and verifies nothing itself. It could check source distributions against the same targets, but those live inside the 140 MB index: building a lookup means decompressing and scanning about 1 GiB (some 7 seconds of CPU, holding the interpreter lock) whenever the index changes, which happens many times a day, and the digests would come from an index whose signatures the cache does not check. That would add nothing to cabal's own check, which cabal always runs, since the cache refuses the unsigned index. A client that is not cabal (curl, a build script) gets the file unchecked. The cache checks only that a source distribution starts with gzip's magic number before it stores it, so an error page sent with 200 is refused (502), and revalidates it daily (see "Storage and eviction"). A corrupt download that passes both, like one of a conda package or a Go module, stays in the store, failing every client's check, until it is evicted or an operator drops it (`forget`, below).
- **Compressed index archives** (opam's `index.tar.gz` and Hackage's `01-index.tar.gz`) are stored byte for byte, as clients expect them. The cache asks for them without a content coding (`Accept-Encoding: identity`) and refuses an answer that names one (502, or the stale copy): it could neither decode such a file nor tell whether the server mislabelled it. Both must start with gzip's magic number. Other index data may arrive compressed and is decoded.
- **Wrong digests.** The cache remembers the actual digests of recent downloads for an hour, so a request naming a digest that contradicts one is refused without downloading the file again.
- **Rewritten index data** is limited to `max_rewrite_size` (default 128 MiB), and at most two rewrites run at once, which bounds the memory and time a very large npm or PyPI document can take.
- **Size limits.** `max_artifact_size` and `max_metadata_size` bound every download.

## Logging and limits

- **Logs.** Every request is logged with ecosystem, path, cache result (hit, miss, revalidated, stale), status, size, upstream time and the sandbox it came from. Package names can carry data out (a lookup for a made-up name still reaches the registry), and the policy middleware's and OpenShell's logs tie each request to its sandbox too.
- **Rate limit.** Upstream fetches are rate-limited by a token bucket shared by all clients (`upstream_rate`, `upstream_burst`), so a flood of lookups for made-up names cannot hammer a registry through the cache.
- **Budgets per sandbox.** Each sandbox also has a bucket of its own (`sandbox_rate`, default 10 fetches a second; `sandbox_burst`, 100), so one sandbox cannot spend the shared budget and stall every other project's installs. The policy middleware names the sandbox in the `X-Sandbox-Science-Sandbox` header of each request it allows to the cache, replacing whatever the sandbox sent, so a sandbox cannot spend another's budget. A request without the header, such as one from the host itself, takes only the shared bucket. The cache keeps buckets for the last 4096 sandboxes it saw. `sandbox_rate = 0` turns the budgets off.
- **Rate limit details.** Every upstream request, each redirect hop included, takes a token: first from the sandbox's bucket, then from the shared one. A request waits up to 10 seconds for each, then gets 503 with `Retry-After: 1`, unless a stale copy can be served. Files the cache already holds cost no tokens.
- **Package downloads.** At most `max_artifact_downloads` (default 16) package files download at once. A request waits up to 5 seconds for a slot, then gets 503 with `Retry-After: 1`. Index data does not need a slot.
- **Wrong digests.** For an hour, the cache remembers the actual digests of each file it downloaded for a digest check, by upstream URL (the last 10,000 files). A later request whose digest contradicts them gets the same 502 at once, without a download. Requests that name different digests for one file take turns, so concurrent ones download it once. A request waits up to 5 seconds for its turn, then gets 503.
- **Upstream connections.** One pool of `max_artifact_downloads` + 32 connections serves every upstream request. Package files never hold more than `max_artifact_downloads` of them, so 32 always remain for index data. Getting a connection, from the pool or by connecting, takes at most 10 seconds, or the request gets 503. A read that stalls for a minute fails, and an index fetch must finish within 2 minutes (for Hackage's 140 MB index, at about 1.2 MB/s or faster). Both get 502 unless a stale copy can be served.
- **Rewrites.** Rewriting index data (PyPI pages, npm metadata) parses the whole document in memory, which takes several times its size and stalls other requests while it runs. A document that is rewritten is limited to `max_rewrite_size` (default 128 MiB, at most `max_metadata_size`). A larger one gets 502, unless a stale copy can be served. At most two rewrites run at once.
- **Clients.** At most `max_requests` (default 1024) requests are served at once. More get 503 with `Retry-After: 1`, except `/-/health` and `/-/stats`. The server closes a connection that has been idle for 15 seconds. A client must accept each MiB of a response within 30 seconds, or its connection is closed, with the stored file it was reading.
- **Open files.** A request being served may hold up to four file descriptors (its connection, the stored file, an upstream connection and a download), and an idle connection one. Run the service with `LimitNOFILE=65536` in its systemd unit. At startup the cache warns when its limit is below 4 × `max_requests` + 1024.
- **Redirects.** Upstream redirects are followed over https only, at most five times. They may lead only to the host of a configured upstream or to a public host. That excludes IP addresses that are not public, `localhost`, single-label and special-use names (`.internal`, `.local`, ...) and anything on the policy's hard floor. Names are checked as written, not as they resolve. Each redirect followed is logged with its target host.
- **Status.** `GET /-/health` and `GET /-/stats` (entries, bytes, hits, misses, revalidations, stale answers, errors) are served on the same listener, and so is `GET /-/opam/archives/<algorithm>/<hex>` (see "opam" under "URL layout"). The middleware refuses all three for sandboxes, since they are not in the URL layout.
- **Sharing.** The store is shared by every project on the host, so timing (a hit or a miss) shows whether some sandbox fetched a file before. Treat package names as visible across projects.

The store lives in `cache_dir`: `blobs/`, the SQLite index `index.sqlite3`, `tmp/` for downloads in progress, and a `lock` that keeps a second server from using the same directory. `sandbox-science-package-cache stats`, `prune` (purge expired negative entries and evict to the cap now) and `forget` (drop one entry) can run beside the server.

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
max_rewrite_size = "128Mi"   # index data the cache rewrites (PyPI pages, npm metadata)
max_temp_size = "16Gi"       # downloads in progress, in tmp/
max_artifact_downloads = 16  # package files downloading at once
max_requests = 1024          # client requests served at once; more get 503
upstream_rate = 20           # upstream fetches per second
upstream_burst = 200
sandbox_rate = 10            # of those, per sandbox (the policy middleware names it)
sandbox_burst = 100

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

[opam]
repository = "https://opam.ocaml.org"

[hackage]
repository = "https://hackage.haskell.org"
```

An ecosystem whose table is absent is disabled. Its paths return 404.

## Client configuration

When the OpenShell backend's `package_cache` setting names the cache's `public_url` ([worker](WORKER.md#settings)), the worker points the package clients in each sandbox at the cache when it starts the executor there. `sandbox_science.worker.package_clients` renders the configuration, and the executor writes it (`configure`, [executor protocol](WORKER.md#executor-protocol)).

- **Files and variables.** The files go in the executor's configuration directory, `<project root>/.executor/config`. A variable added to every shell command and kernel points each client at its file. uv reads neither pip's file nor its variables, so it gets a variable alone. Cargo reads configuration only from `$CARGO_HOME` and from `.cargo` directories above the working directory, and `CARGO_HOME` also holds Cargo's caches and installed programs, so Cargo's file goes in the home directory, `~/.cargo/config.toml`. Cargo does not look there if the image sets `CARGO_HOME`.
- **Changes are kept.** The executor records what it wrote. It leaves alone a file that someone changed since, or that it did not write, and the worker logs a warning naming the file.
- **The cache only.** The configuration names no public registry and no fallback: no extra index, no `defaults` channel, no `direct` in `GOPROXY`. Go still checks every module against the checksum database, through the cache.
- **Not covered yet.** pixi and yarn 2+ read files of their own. pnpm and yarn 1 read `.npmrc` files, but were not tried with this one. opam and cabal come with their adapters.
- **Real clients.** `SS_INTEGRATION=1 uv run pytest packages/worker/tests/test_package_clients_integration.py` has an executor write this configuration, then installs a small package with pip, uv, conda, mamba, micromamba, cargo, go and npm, without registry options, through a cache it starts on loopback. Every other request goes to a trap that fails the test. It needs access to the public registries.

Where `<cache>` is the cache's `public_url`, the clients get:

| Client | Setting |
| --- | --- |
| pip | `pip.conf`, named by `PIP_CONFIG_FILE`: `index-url = <cache>/pypi/simple/`, and `trusted-host` for the cache's host while the cache is plain HTTP |
| uv | `UV_DEFAULT_INDEX=<cache>/pypi/simple/`. A `uv.toml` named by `UV_CONFIG_FILE` would replace the project's own uv settings |
| conda, mamba, micromamba | `condarc`, named by `CONDARC`: `channel_alias: <cache>/conda`, `channels: [conda-forge]`, and `default_channels: []`, so `defaults` names nothing. mamba and micromamba then read only this file, which also hides `mirrored_channels` in Miniforge's own `.condarc`; conda reads its other `.condarc` files too, below this one |
| cargo | `~/.cargo/config.toml`: `[source.crates-io] replace-with = "package-cache"`, `[source.package-cache] registry = "sparse+<cache>/cargo/index/"`, and `[http] multiplexing = false` while the cache is plain HTTP: Cargo then asks to upgrade the connection to HTTP/2 (`Upgrade: h2c`), which OpenShell refuses on inspected endpoints (403 `unsupported_l7_protocol`) |
| go | `go.env`, named by `GOENV`: `GOPROXY=<cache>/go`, `GOSUMDB=sum.golang.org` (checksum lookups go through the proxy), `GOTOOLCHAIN=local` |
| npm | `npmrc`, named by `NPM_CONFIG_USERCONFIG`: `registry=<cache>/npm/`, `audit=false`, `fund=false`, `update-notifier=false` |
| yarn 2+ | `npmRegistryServer: <cache>/npm` and `unsafeHttpWhitelist` for the cache's host in `.yarnrc.yml` (yarn ignores `.npmrc`) |
| opam | the repository `default` at `<cache>/opam` (`opam init --bare default <cache>/opam`, or `opam repository set-url default <cache>/opam` in an existing root); its `archive-mirrors` then send archive downloads to `<cache>/opam/cache` |
| cabal | in its config file, `repository hackage.haskell.org` with `url: <cache>/hackage/` and `secure: True`, and `http-transport: plain-http` while the cache is plain HTTP. The name keeps the Hackage root keys that cabal ships; with a transport that supports https (curl, wget), cabal would upgrade that repository's URL to https, which the cache does not offer |

## Toolchains

Compilers and package managers come from conda-forge through the cache where conda-forge has them: `ocaml`, `ghc` and `cabal` (cabal-install). conda-forge's newest GHC is 8.10.7; its package installs the global package database without its cache, so run `ghc-pkg recache` once before cabal builds anything with it. Use opam 2.5.2 or later, and 2.6.1 or later in the 2.6 series: opam.ocaml.org's `repo` file warns that older versions lack security fixes, and marks 2.6.0 out of date. conda-forge's newest opam is 2.5.0 (checked 2026-10-11), so opam comes from the opam project's GitHub releases instead, as a static binary checked against the SHA-512 that opam's `install.sh` publishes. Newer GHCs come from GHCup. Both are ordinary HTTP, decided by the network rules. An opam switch can also build OCaml from opam-repository (`ocaml-base-compiler`), and then the compiler's source comes through the cache like any other archive.

## Running it

```bash
uv run sandbox-science-package-cache serve --settings cache.toml
```

- **On babbage (VM driver).** Each sandbox's supervisor runs on the host, so the cache listens on loopback (`listen = "127.0.0.1:8090"`). Sandboxes reach it as `host.openshell.internal:8090`, which is also its `public_url`. Name it that way in the OpenShell targets (`package_cache: {host: host.openshell.internal, port: 8090}`). The traffic never leaves the host, so plain HTTP is acceptable there.
- **Inspecting and pruning.** `stats` prints the counters and store size as JSON. `prune` evicts down to the cap now. `forget --settings cache.toml KEY_OR_PATH` drops one entry, such as a corrupt copy of a file the cache cannot verify, so that the next request fetches it again. It takes a cache key or the path a client requested, as the request log shows it (`/hackage/package/split-0.2.5.tar.gz`). All three run beside a serving cache.
- **Real clients.** `SS_INTEGRATION=1 uv run pytest packages/package-cache/tests/test_clients_integration.py` installs a small package with pip, uv, mamba, cargo, go, npm, opam and cabal through a cache it starts on loopback. It needs access to the public registries. opam builds an OCaml compiler from source unless `ocaml` is on the PATH; cabal updates its index (once in full, once by a byte range) and downloads a package, and builds it only when `ghc` is on the PATH.

## Adapters

Each ecosystem is an adapter (`sandbox_science.package_cache.adapter`). Given the request path below its prefix, it returns one of three answers:
- a `Fetch`: the upstream URL, cache key, kind, whether the result is immutable, the request headers to send, the expected digest, the magic number the body must start with, for index data a rewrite function, a shorter lifetime (`max_age`) and whether it is stored byte for byte (`exact`), and for artifacts without a digest when to revalidate them (`revalidate_after`);
- a `Static` response it builds itself;
- `None`, a 404, for anything it does not recognize.

Adapters are pure functions of the path, the request headers and their settings, so they are tested without a network. The core does all I/O.

## Planned

- A cooling-off period: refuse versions published in the last N days unless approved. PyPI, conda, Cargo, Go and npm publish release times, and Hackage's index records when each version was uploaded; opam's index records none. The middleware can pass the project's setting to the cache in a request header that it writes itself, replacing anything the sandbox sent, as it does the sandbox's name for the budgets; the cache must accept the setting only from the middleware.
- conda index files are passed through unchanged because conda-forge's base URLs are relative (empty). The cache should refuse index files whose `base_url` is absolute, in case a channel changes that. Likewise, opam's index is passed through unchanged because opam.ocaml.org's `repo` file names its archive cache relative to the repository and redirects only opam 1. The cache should refuse an index whose `repo` file names an absolute archive mirror or redirects opam 2.
- Keeping files that active lockfiles use from eviction.
- Lockfiles that travel. A lockfile written in a sandbox names the cache: npm records the rewritten download links, and `uv.lock` the cache's index and file URLs. It fails outside a sandbox, or after `public_url` changes. `Cargo.lock`, `go.sum`, cabal freeze files and opam lock files are unaffected. The worker's own lockfiles of environment revisions name Conda channels, and PyPI files by their upstream URLs, never the cache's address ([worker](WORKER.md#package-commands)).
- TLS between OpenShell and the cache, for deployments where the cache is not on the gateway's host.
