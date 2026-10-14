# Compatibility

How the orchestrator, the workspace and the worker stay compatible, and how a request keeps its identity across releases. Owner decision 8 of the [refactoring plan](../.agents/refactor-plan/PLAN.md) sets the policy until M3.

## Lockstep releases

The orchestrator, the workspace and the worker of one release are upgraded together, with the shared `contracts` and `policy` libraries. Nothing negotiates versions. Every contract model refuses fields it does not know, in requests and responses alike (`Contract` in `packages/contracts`, `extra="forbid"`), so a new field breaks whichever side does not know it, in either direction. The version check turns that into one clear error on the first request:

- `PROTOCOL_VERSION` in `contracts/worker.py` is the worker API's version (3). It changes with any change to what the worker or its clients send.
- Every response from the worker's application carries it in the `X-Worker-Protocol` header, errors and event streams included. `Capabilities.protocol` carries the same number. Errors that never reach the application carry no header: aiohttp's own refusals of a malformed request, and answers from a proxy or TLS terminator in front of the worker.
- The worker client (`orchestrator/worker_client.py`), which the harness and the workspace both use, and the workspace's provisioning observer refuse a response with another version, or a successful one without the header. The error is a `WorkerProtocolError` of kind `unsupported` ("version mismatch: the worker reports protocol 2, this client speaks protocol 3; upgrade the orchestrator, workspace and worker together"). The client does not retry it, and no caller reads it as a feature the worker lacks. An error status without the header is reported as that HTTP error instead: `unavailable` for 502, 503 and 504, `internal` otherwise.
- The clients send the header too. The worker refuses a request that names another version with the same `unsupported` error (HTTP 422), before it reads the body. A request without the header, such as one written by hand, is served.

The executor ships inside the worker package, so its protocol with the worker is internal to one release. A policy that uses a new policy field, such as `packages.agpl_exceptions`, needs a policy library that knows it in the worker, the policy middleware and the MCP proxy, so they are upgraded together too.

## Request identity

A client names each operation with an ID of its own. The worker stores the request's digest with the operation; the same ID sent again with the same request returns the same execution or provisioning operation, and with a different request it is a `conflict` ([tools spec](specs/tools.md#lifecycle-idempotency-and-reconnection)).

The digest is `request_digest` in `contracts/canonical.py`: SHA-256 over a domain naming the request type (`worker.execution.<kind>`, `worker.provision`) and the sorted-key JSON of the fields whose values differ from their defaults. Therefore:

- A field added with a default leaves every stored identity unchanged, as long as the default means what omitting the field meant before.
- A value equal to the default is the same request as omitting it. A choice such as "use the policy's default" is therefore its own value: `JobRequest.timeout_seconds` is `None` for the policy's default, which differs from asking for 24 hours.
- Operations stored before protocol 2 hold the older digest, pydantic's JSON of the whole request. The worker still accepts it (`worker/identity.py`), so they replay across the upgrade. That fallback goes once no worker state from before protocol 2 remains.

## Changing a contract

- Any change to what crosses the worker API raises `PROTOCOL_VERSION`.
- A new request field gets a default that means the old behavior.
- Changing a default's meaning, or how a field is written in JSON, changes the identity of stored operations. Such a change keeps accepting the old digest for one release, as `worker/identity.py` does now.
- The worker stores contract JSON as its records and validates it again on read, so a stricter validator can make stored records unreadable. Plan items C-07 and C-08 replace this with a versioned store.

## Before independent upgrades

When components must be upgraded independently (M3 placement), the policy changes: responses accept unknown fields (`extra="ignore"`), requests stay strict, changes are additive only, new behavior is gated by `Capabilities.features`, and the version check accepts a range. None of this is built.
