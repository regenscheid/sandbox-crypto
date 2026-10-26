# OpenShell: a `text/event-stream` response is never offered for whole-body inspection

**Status.** Filed 2026-10-26 by the owner's request as [NVIDIA/OpenShell#4929](https://github.com/NVIDIA/OpenShell/issues/4929). Observed on babbage on 2026-10-26 by the direct MCP lab ([labs README](../labs/README.md#direct-mcp-path-against-the-owners-portal-d98-a2)), against a real Cloudflare MCP portal.

**Affects.** OpenShell v0.1.5, a sandbox with an `mcp` endpoint and a supervisor middleware (`network_middlewares`) that handles HTTP responses.

## Summary

A supervisor middleware that handles responses is first sent an `HttpResponsePreflight`, which lists the body modes it may choose in `permitted_body_modes` (`HEADERS_ONLY`, `WHOLE_BODY_BYTES`, `STREAM_BYTES`) and a `max_payload_bytes`. For a `text/event-stream` response, `WHOLE_BODY_BYTES` is never in that list, whatever the size of the stream. The middleware can inspect the pieces as they stream or nothing at all, so it can neither read nor rewrite a response that arrives as an event stream.

This matters for MCP. The Streamable HTTP transport lets a server answer any POST either with `application/json` or with a `text/event-stream` that carries the JSON-RPC response as one `message` event and ends. A server chooses; a client has to accept both. A middleware that reads a `tools/list` answer, to leave out the tools a policy denies or to check the tools' schemas against pins, never gets the listing when the server chooses an event stream, because a listing cannot be rewritten in pieces.

## Steps to reproduce (outline)

No private address or credential is needed. Any MCP server that answers `tools/list` as an event stream will do, for instance a small local server on the host built with an MCP SDK's Streamable HTTP transport with JSON responses off (the default in some SDKs), reached from the sandbox through the host alias.

1. Check from the host that the server's `tools/list` answer (after `initialize` and `notifications/initialized`, with `Accept: application/json, text/event-stream`) has `Content-Type: text/event-stream`, one `event: message` whose `data:` is the JSON-RPC response, and then ends. Make a second server, or a switch on the first, that answers the same request as `application/json`.
2. Give a sandbox a policy with an `mcp` endpoint to the server's host and port and a supervisor middleware for it whose handler logs the `permitted_body_modes` of each `HttpResponsePreflight` and the `content-type` it came with, and then chooses `WHOLE_BODY_BYTES` if it is offered.
3. From the sandbox, send the `tools/list` request through the gateway's proxy, to the JSON server and to the event-stream server.

**Expected.** For both, `WHOLE_BODY_BYTES` is among the permitted modes, and the middleware receives the whole answer and may replace it. For an event stream, the offer is bounded as it is for JSON: made when the stream ends with its response within `max_payload_bytes`. One that does not end, or is over the bound, is not offered the mode, as now.

**Actual.** For `application/json`, `WHOLE_BODY_BYTES` is offered. For `text/event-stream` it is not offered, and a middleware that needs the whole body can only refuse the response. In the lab, every `tools/list` answered by the Cloudflare MCP portal (it answers in event streams) was refused by the Sandbox Science middleware with its reason code `tool_listing_unreadable`, so the agent was offered no tools.

## Impact

- **A middleware cannot enforce on what a server lists.** Dropping denied tools from a listing, and checking a tool's description and schema against a pin so that drift is noticed, both need the whole listing. Servers that answer in event streams are out of reach of them.
- **No client-side workaround.** The client may send `Accept: application/json` alone, but the transport lets the server ignore it, and the portal in the lab does.

## Suggested fix

Offer `WHOLE_BODY_BYTES` for a `text/event-stream` response when the stream ends with its response within `max_payload_bytes`: buffer it until it ends, give the middleware the whole body, and send the middleware's replacement (or the original) to the client as an event stream. A stream that outlives the bound is not offered the mode, as now. An alternative that also serves this case is a mode that gives the middleware one complete SSE event at a time.

## Our mitigation

The middleware blocks a listing it cannot read (`tool_listing_unreadable`) and decides each call by its name and arguments, which needs no listing. The MCP proxy reads and bounds the answers itself, so a deployment whose server answers in event streams uses the proxy until this is fixed ([MCP connectors](../../docs/MCP-CONNECTORS.md)).
