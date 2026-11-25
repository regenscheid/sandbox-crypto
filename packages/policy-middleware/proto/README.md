# Vendored OpenShell contracts

`supervisor_middleware.proto` and `extension.proto` are copied unmodified from
[ai-openshell/openshell](https://github.com/ai-openshell/openshell) (formerly
`NVIDIA/OpenShell`) tag `v0.1.10` (commit `152c59d31780`), directory `proto/`.
`extension.proto` is unchanged since `v0.1.5`; `supervisor_middleware.proto` adds the
streaming `HttpHooks` service and marks the legacy HTTP hooks the middleware implements
(`EvaluateHttpRequest`, `HttpResponsePreReturn`) deprecated, with their messages unchanged
on the wire. They are licensed under the Apache License 2.0, reproduced in
[LICENSE](LICENSE); the copyright notices are in each file.

| File | SHA-256 |
| --- | --- |
| `extension.proto` | `60f28ee9dc846d41152f4db7e82a439c78d024786efaceb464900d1d1ad129ad` |
| `supervisor_middleware.proto` | `62716896a688dafce2b221624d6e603379226e09dfc78dfc3417f614d46cd559` |

The Python code in `src/sandbox_science/policy_middleware/_proto` is generated from
them by `uv run python scripts/generate_protos.py`; CI checks that it is current.
Update these files together with the OpenShell version the middleware targets.
