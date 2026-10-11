# cabal-install: hackage-security's Cache-Control headers are never sent

**Status.** Kept here, not filed upstream: filing is the user's call. Found on 2026-10-11 while adding Hackage to the package cache, by reading the source and by watching cabal's requests through a logging proxy. The retry case (`max-age=0`) was read from the source, not reproduced.

**Affects.** cabal-install 3.14.2.0 (tag `cabal-install-v3.14.2.0`) and the master branch as of 2026-10-11, with hackage-security 0.6.2.6. Paths below are relative to the cabal repository.

## Summary

hackage-security asks for `Cache-Control: no-transform` on every request to a secure repository, and adds `max-age=0` when it retries after a verification error, so that caches between the client and the server neither transform files nor answer the retry with the same stale copy. cabal-install's adapter from hackage-security's `HttpLib` to its own HTTP transports builds request headers with a helper that only modifies entries already in its list. The list starts empty, so neither header is ever sent, whichever transport is used.

## Steps to reproduce

1. Point a secure repository at a proxy that logs request headers, for example:

   ```
   repository hackage-proxy
     url: http://127.0.0.1:18101/
     secure: True
     root-keys: <Hackage's root keys>
     key-threshold: 3
   ```

2. Run `cabal update` with `http-transport: curl` or `plain-http`.

**Expected.** Every request carries `Cache-Control: no-transform`, and requests after a verification error also carry `max-age=0`.

**Actual.** No request carries `Cache-Control`. With `-v2`, the curl transport's command lines show `--header 'Range: …'` for the index update and no other header.

## Where it happens

`mkReqHeaders` in `cabal-install/src/Distribution/Client/Security/HTTP.hs` (lines 137-171 at the 3.14.2.0 tag) folds hackage-security's headers into an association list with `insert x y = modifyAssocList x (++ y)`. `modifyAssocList` returns `[]` for an empty list (`go [] = []`) and otherwise only changes an existing entry, so `insert` never adds one. hackage-security asks for the headers in `httpRequestHeaders` (`hackage-security/src/Hackage/Security/Client/Repository/Remote.hs`, lines 241-248 in 0.6.2.6).

## Impact

- **Caches can answer retries with stale files.** hackage-security relies on `max-age=0` to get past caches that served files from different moments (a new `timestamp.json` with an older `snapshot.json`). Without it, every retry gets the same files, and `cabal update` fails after five attempts. Sandbox Science's package cache works around this by revalidating Hackage's metadata on every request (docs/PACKAGE-CACHE.md).
- **Transforming proxies are not told to leave files alone.** A proxy that recompresses the index would break its hash.

## Suggested fix

Make `insert` add a missing key, for example with `Map.insertWith (flip (++))`, or build the header list directly. A test that checks the headers `mkReqHeaders` produces for `[HttpRequestMaxAge0, HttpRequestNoTransform]` would have caught it.
