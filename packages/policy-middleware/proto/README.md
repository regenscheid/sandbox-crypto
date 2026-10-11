# Vendored OpenShell contracts

`supervisor_middleware.proto` and `extension.proto` are copied unmodified from
[NVIDIA/OpenShell](https://github.com/NVIDIA/OpenShell) tag `v0.1.3` (commit
`e1f3c82caa3e`), directory `proto/`. They are licensed under the Apache License 2.0,
reproduced in [LICENSE](LICENSE); the copyright notices are in each file.

| File | SHA-256 |
| --- | --- |
| `extension.proto` | `60f28ee9dc846d41152f4db7e82a439c78d024786efaceb464900d1d1ad129ad` |
| `supervisor_middleware.proto` | `bee96ac940303d8a7530e6e2283a98077bb428a05ef610b51d018ea18377bebd` |

The Python code in `src/sandbox_science/policy_middleware/_proto` is generated from
them by `uv run python scripts/generate_protos.py`; CI checks that it is current.
Update these files together with the OpenShell version the middleware targets.
