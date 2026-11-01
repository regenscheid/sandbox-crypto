# Vendored OpenShell contracts

`supervisor_middleware.proto` and `extension.proto` are copied unmodified from
[NVIDIA/OpenShell](https://github.com/NVIDIA/OpenShell) tag `v0.1.5` (commit
`bdb956f2cd65`), directory `proto/`. They are licensed under the Apache License 2.0,
reproduced in [LICENSE](LICENSE); the copyright notices are in each file.

| File | SHA-256 |
| --- | --- |
| `extension.proto` | `60f28ee9dc846d41152f4db7e82a439c78d024786efaceb464900d1d1ad129ad` |
| `supervisor_middleware.proto` | `b3c2037a6b51285b48a39025b85e79a4c717a2b95accb89cf43dbe6079831c75` |

The Python code in `src/sandbox_science/policy_middleware/_proto` is generated from
them by `uv run python scripts/generate_protos.py`; CI checks that it is current.
Update these files together with the OpenShell version the middleware targets.
