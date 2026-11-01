# Workspace document, comment and job API contracts

The document, comment and background-job clients share the workspace HTTP transport.
Their browser types are generated from server schemas. `api.ts` continues to export
these types and methods so existing callers do not need a broad migration.

## Schema directions

Existing model responses use serialization schemas and retain required fields,
including defaults emitted by `model_dump(mode="json")`. Request unions use the
existing action validators in validation mode, with wire aliases: defaulted request
fields remain optional, nullable fields remain nullable, and `replace_text` uses
`with`, not the Python attribute `with_`. Document and annotation action validation
and domain checks remain authoritative on the server.

Responses assembled as dictionaries have explicit Pydantic-supported TypedDict
schemas. Required keys describe fields always emitted by those routes;
`NotRequired` describes actual omissions. These schemas describe the wire payload
without reconstructing it or adding defaults. Compact stored author provenance,
null-stripped history patches, optional comment body sources and replay markers,
and empty job log objects remain supported. JSON-valued extension data stays JSON;
it is not replaced with an untyped mutation dictionary.

The generator handles these dictionary schemas separately from existing response
models. A name collision with differing schemas fails generation. Generated
TypeScript is a compile-time contract, not runtime browser validation.

## Route compatibility

URL encoding, query defaults and cursor omission, response status and JSON bodies,
source export, scope checks, read-only reads, USER write attribution, same-origin
protection and domain validation remain unchanged. Job cancellation continues to
accept the existing bodyless request and returns its existing brief receipt rather
than the detailed listing shape. The job view explicitly handles retained waits
without a start time or wait mode. Run records and other API domains are outside this
change.

## Verification

Public route tests characterize representative document, history, patch, comment
and job responses and validate their schemas without changing payloads. Generator
tests cover response defaults, request defaults and aliases, optional response
keys, name collisions and freshness. TypeScript compile-time checks exercise the
request action unions, and focused client tests preserve URLs and POST payloads.
The existing workspace tests, strict Python checks, frontend checks and frontend
tests verify compatibility with consumers.
