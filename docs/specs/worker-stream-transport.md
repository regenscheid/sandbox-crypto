# Bounded OpenShell worker request transport

The OpenShell backend uses `openshell sandbox ssh-config NAME` to obtain a private,
per-connection SSH configuration, never installing it in the user's SSH files.
The worker host requires the OpenSSH `ssh` executable. OpenSSH runs without a PTY, forwarding or login shell (`OPENSHELL_NO_LOGIN_SHELL=1`),
with the same fixed Python, uploaded executor files, shim and daemon identity.
This bypasses OpenShell 0.1.5's cumulative 4 MiB `sandbox exec` stdin ceiling.
OpenShell requires daemon mode; `daemon=false` is explicitly unsupported before
upload/start. Local stdio and local daemon transports retain their existing format.

SSH alone is insufficient: the supervisor has bounded process-stdin buffering.
Each original JSON request is therefore encoded as stop-and-wait JSON chunk frames,
at most 64 KiB per line including newline, carrying a random transfer identity,
original integer request ID, monotonic sequence, final flag and base64 data. There
is at most one unacknowledged frame and one assembling request per connection.
The bridge acknowledges a frame only after consuming it into bounded retained
storage, or (for the final frame) validating and draining exactly one complete
original request to the authenticated daemon. ACK is transport receipt, never
execution success. Replies/events and ACKs serialize their stdout writes.

The bridge validates exact frame keys/types, transfer/sequence/ID continuity,
base64, total size at most `executor.MAX_REQUEST`, complete JSON object and original
request ID. A malformed, oversized, interrupted or disconnected transfer dispatches
no partial original request and closes the bridge. It cannot be completed on a new
connection. Existing daemon authentication, incarnation fencing, recovery journal
and event replay remain authoritative. No arbitrary worker effect is introduced.

The worker serializes complete transfers, retains an absolute write deadline, and
checks cancellation and current effect authority before the final frame. A sender
cancelled after any frame closes that connection rather than reuse partial state.
ACK processing is independent of bounded ordered event callbacks (128 events and
32 MiB), so callbacks may issue requests without blocking transport progress. Normal
replies wait for preceding event callbacks, preserving replay commit order; only
callback-issued requests bypass their own event barrier while that callback is
still active. Inherited background context expires with the callback. Close drops
queued callbacks, drains the already-started callback and stops the old bridge
before notifying recovery; callbacks cannot commit beside their replacement. Callback backlog overflow
closes the connection; daemon replay recovers retained events. SSH configuration
files are removed on startup failure and channel closure. Existing atomic shared
file writes, create-only publication and hash locks are unchanged.

Deterministic validation covers slow consumers, requests larger than 4 MiB,
cumulative transfers, malformed IDs/sequence/base64/size/final data, disconnect,
concurrent calls, ACK/event interleaving, cancellation and daemon reconnect. Actual
OpenShell bulk-transfer qualification is separate from these tests.
