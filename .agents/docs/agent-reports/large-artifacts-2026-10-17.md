# Large-artifact acceptance, 2026-10-17

Local implementation: `9a792d85` (durable blobs, schema 68) and `bd738a9a`
(streamed workspace downloads). The confined runtime uses control protocol 2.
The tested image was `sandbox-science/orchestrator:acee67a0`, revision
`acee67a0ab1ffa0a3a79bfb730a38f268c7f6378`, manifest digest
`sha256:fc035be0059304ebca73a70ffb99e3194f4292cb29bddcaf3a6571e8f5351b14`.

Deterministic acceptance passed 149 artifact/HTTP/download tests and 139 adjacent
control/provenance/review/image/PDF/export tests. Coverage includes exact 1 GiB
stream ingestion with measured Python peak below 4 MiB, external content rather
than a SQLite payload, legacy reads, scoped references, corruption refusal,
quota reservations, duplicate completion, and installed-orphan accounting. These
are focused checks, not a claim that every production process uses less than 4 MiB.
Independent source review found no remaining medium-or-higher issue in that slice.
The final download module passed 10 tests, including a filesystem blob larger than
8 MiB; frontend type checks, formatting and production builds passed.

Live local acceptance exercised a 100 MiB user upload through framed admission
and worker retrieval. The saved run contains a successful worker result measuring
104,857,600 bytes and SHA-256
`4cbf988462cc3ba2e10e3aae9f5268546aa79016359fb45be7dd199c073125c0`
in 100 bounded blocks, followed by an applied living-document patch. The original
upload remains separate from the small verification-result artifact.

A delegated child produced retained artifact `artifact-7a6dfc44642b4126a9252281`,
revision 1: 536,870,912 bytes (512 MiB), SHA-256
`c047731a3c134f3d34286d608e9c173027d50f43ab9d2064f3c360939977e908`.
The streamed workspace API download matched the generated fixture's size and SHA-256;
an exact byte-range comparison also passed. The deletion receipt confirms removal of the exact
local worker VM while other sandboxes remained. A post-deletion download evidence
file records successful full and range verification of the same retained revision.

The download evidence records `/proc` client `VmHWM` and `VmRSS` of 64,688 kB.
This is one client-process observation, not a whole-host memory bound. Inherited
pre-exec `getrusage().ru_maxrss` is excluded as unreliable for this measurement.

This qualifies the local filesystem backend adjacent to the control database and
download after worker deletion. It does not qualify S3, PVC migration, host-loss
recovery, or durability after deleting the control database and its blob directory.
Runtime image checks passed in container and Landlock modes; those checks alone
are not live research acceptance.

Raw evidence stays outside Git. Basenames and SHA-256 digests:

| Evidence | SHA-256 |
| --- | --- |
| `upload.json` | `00364269cdb92c2f630473873a4d7771200f78b5c92b8c0073ce28897307de73` |
| `upload-framed-admission.json` | `62ae2efb685e1f8407a7fdc1c9151a0a5d0a2090db11029439fd8545afef2e86` |
| `20261017T190709967192Z-run-498ef1a9b15f532680d6c8e8.json` | `a29331fee96f7b62ed618973879b04b75137530fffe60bb72c28c20a6834ba4b` |
| `large-artifact-download.json` | `8e0461613a64d5b2c99f7b096b789924bb5434fb3d0ccc107db393317adfc4ae` |
| `large-artifact-download-after-delete.json` | `8e0461613a64d5b2c99f7b096b789924bb5434fb3d0ccc107db393317adfc4ae` |
| `after-local-worker-delete.json` | `2224bb8e5a4d7774514d371911b7f6ab5e37640ed8c1834ecb2caee57420df0e` |
| `image-blobs-checks.log` | `6a731714d35d86cd189558b1ba8193b1402de1f4c93ef421db2ceae3dd6b8e19` |
| `build-blobs.log` | `f8ba1e52b9c748ec4ebaa07fccb8dd07632548169cf9d4f3c69a7eecd189fe43` |

The coordinator confirmed sequential execution: delete the exact VM, assert its
absence and save the deletion receipt, then rerun the authenticated full/range
download. That rerun overwrote `large-artifact-download.json` and was copied to
`large-artifact-download-after-delete.json`. Their identical digests represent one
post-deletion measurement, not independent before/after measurements. The earlier
pre-deletion download result was overwritten and is not used for memory figures.
