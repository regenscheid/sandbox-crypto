# Kubernetes runtime bootstrap

Status: Design with owner decisions (2026-10-22); not built. It closes the gate in
[the offline bundle](../KUBERNETES-BUNDLE.md#render-inspect-then-qualify-separately)
that says "cluster runtime bootstrap is an unimplemented gate". It applies
[orchestrator confinement](orchestrator-confinement.md#launcher-and-replacement),
[Kubernetes operations](kubernetes-operations.md#first-deployment-bundle) and
[research continuity](research-continuity.md#orchestrator-boundary) (RC-26 to RC-29,
RC-F20) to the cluster. Requirements are numbered KRB-n. OpenShell facts come from
the v0.1.5 source; file references are paths in that tree.

## Context

Every agent and model loop runs in one OpenShell sandbox from the `orchestrator`
image. On babbage the host launcher (`launcher/orchestrator.py`) writes the policy, a
fresh `runtime-<24hex>` generation, a rotated control token, `control.json` and
`runtime.json`; records the name before create and the immutable ID before Ready;
uploads token, model key and settings to `/sandbox/runtime`; and holds an `openshell
sandbox exec` session running the runtime. Explicit replacement (`replacement.py`) and
bounded recovery (`infrastructure.py`) reuse the identity-checked `delete_recorded`.
Nothing does this in the rendered bundle. Control is cluster-ready: TLS, optional CA,
and token and generation files reread on every frame and at handshake
(`confinement/server.py`). One runtime process serves one control connection with
one generation and exits when it ends (`runtime.py`).

On Kubernetes, OpenShell 0.1.5 makes each sandbox two Pods (`docs/kubernetes/setup.mdx`
19-32): a **workload Pod** (runs `sleep infinity` and our exec'd runtime; new
connections from it are denied) and a **supervisor Pod**, which holds the gateway
session and opens every approved upstream connection. The runtime therefore reaches
control from the supervisor Pod's address. RuntimeClass, node selector and template
labels go on the workload Pod only (`crates/openshell-driver-kubernetes/src/driver.rs`
~6531-6584); the supervisor Pod gets fixed OpenShell labels and neither of the others
(`src/sandbox_runtime.rs` 213-497, 636-668).

## Non-goals

No agent loop, runtime CLI or model call in a trusted Pod. No research or MCP sandbox
lifecycle (that stays with the worker). No multiple runtimes, app replicas or
failover; no public ingress, image publication, ARM64 Kata or operator/CRD; no
Kubernetes API use by the manager; no change to the control protocol, generation
fencing or attempt model. Starting sandboxes on demand without an operator command is
a later direction (backlog), not this design.

## Placement and authority

- KRB-1. **A `runtime-manager` Deployment of its own:** one replica, `Recreate`, any
  node (arm64 or amd64; the image carries both, or the renderer pins the one it has),
  `automountServiceAccountToken: false`, no Service or listener. It alone creates,
  attaches to, replaces or deletes the runtime sandbox, and runs no agent loop or
  model call. The app Pod (control and workspace) never sees the gateway credential.
- KRB-2. **Its gateway credential.** The manager Secret holds an OpenShell CLI profile
  with its own mTLS client certificate, the model key, the control CA, its control
  bearer token and `runtime-manager.json`; a copy init builds the CLI's XDG tree
  (`openshell/active_gateway`, `gateways/<name>/metadata.json`, `mtls/*`) in a memory
  volume. This amends the bundle doc's rule that only the worker holds one. **Authority
  (Q2):** a verified certificate becomes a user principal, CN as subject and OU values
  as roles (`crates/openshell-server/src/multiplex.rs` `extract_peer_identity`
  ~1417-1450). Without OIDC there is no authorization policy and the admin role is
  empty (`lib.rs` ~433), so every certificate user is platform admin in every
  workspace (`auth/workspace_authz.rs` `authorize_workspace`, `is_platform_admin`).
  A separate certificate is distinguishable in audit (`auth/principal.rs`) but the
  server has no revocation list: revoking it means replacing the client CA. Scoping
  exists only in OIDC RBAC mode (`auth/authz.rs` `check`): a certificate whose OU is
  the user role must then be a member, by CN, of the workspace it acts in, and
  workspace User suffices to create, get and delete sandboxes (`grpc/sandbox.rs`). A
  runtime-only workspace would confine the manager (unqualified; R1).
- KRB-3. **The manager acts only on its own record:** a recorded name matching
  `ss-orch-[0-9a-f]{10}` for which the gateway reports the recorded ID. With full
  gateway authority this guards against a corrupt record; it is not authorization.
- KRB-4. **Network**, default deny. Manager Pod: no ingress; egress to the app Pod's
  TCP 8444 (KRB-7), `network.gateway` and `network.dns`. App Pod: ingress 8444 only
  from the manager Pod's labels; 8443 from `network.runtimes` as today; no gateway
  egress. `network.runtimes` must select **supervisor** Pods. **Labels (Q3):**
  `allowed_pod_labels` lets a sandbox template set allowlisted keys outside
  `openshell.ai` and `agents.x-k8s.io` (`src/config.rs` 275-287, 891-896; `driver.rs`
  `validate_pod_metadata` ~6919-6941), but only on the workload Pod. The supervisor Pod
  carries only `openshell.ai/{managed-by,sandbox-id,component,gateway-id}` and
  boundary-pair labels; the sandbox ID is known only after create and the renderer is
  static. The narrowest peer is the sandbox namespace plus
  `openshell.ai/component=supervisor` and this gateway's `openshell.ai/gateway-id`,
  so every supervisor of the gateway reaches 8443; each sandbox's OpenShell policy
  (only `compile_orchestrator` permits control) and the token remain the gates (R2).
- KRB-5. **Kata only (tier 3).** The manager refuses a runtime profile without the
  Kata `runtime_class_name` and an `amd64` node selector, and creates with
  `--driver-config-json` from it. OpenShell applies the RuntimeClass to the workload
  Pod; the supervisor runs under the cluster default runtime. The gateway enables
  `allow_driver_config`; the RuntimeClass carries `openshell.ai/sandbox-attachable=true`
  ([gateway settings](../KUBERNETES.md#gateway-settings-for-profile-placement)). The
  driver JSON is recorded with ownership; a replacement reuses it unless an explicit
  request names another.

## Credential handoff: control is the authority

- KRB-6. **Control mints and records every generation and token.** The token lives in
  a memory `emptyDir` mounted only into control. The generation and an issue journal
  (request ID, generation, token SHA-256, state) live in control's state directory,
  mode 600, written atomically. `control.json` points `runtime_token_file` and
  `generation_file` there, so the per-frame token reread and handshake generation read
  are unchanged; its static `generation` becomes a placeholder never issued. At
  process start control writes a fresh unissued token and marks the last issue
  superseded, so a control restart revokes the runtime. Only the digest is durable.
- KRB-7. **A narrow manager endpoint.** A second TLS listener (TCP 8444, same
  certificate, on the control Service) serves only `GET /health` and `POST
  /runtime/credentials`; the runtime listener does not serve the latter. It exists only
  when `control.json` names `manager_token_sha256_file`, so the host is unchanged.
  The manager's bearer token is compared by digest in constant time; failure answers
  404. Body `{"request": "<32 hex>"}`; requests are serialized:
  1. The latest request, still issued and current: the same generation and token.
  2. An older or superseded request: 410, no token. Control never returns an older one.
  3. A new request: journal it `revoked` and replace the token with a fresh unissued
     value, which refuses any connected runtime at its next frame. If a runtime is
     connected or draining, answer 409; the manager retries the same request once
     `/health` shows both false.
  4. Otherwise mint `runtime-<24hex>` (never a journaled value) and a 32-byte token;
     write generation, then token, then journal `issued`; return both. A crash before
     the journal write leaves the request `revoked`, so a retry mints fresh values.
- KRB-8. **Why control mints.** Control already owns durable state and fences
  generations, so "never older, never while occupied" is enforced in one place. A
  manager-pushed token would need an endpoint that lets the caller choose values, plus
  the same checks. The manager keeps no lasting runtime capability, and the gateway
  credential and control state stay in different Pods. A stolen manager token, usable
  only from the manager Pod's address, can revoke the runtime and get a token for a
  new one: what the manager may do anyway.

| Event | Behavior |
| --- | --- |
| Manager crash after issue, before upload | The manager journals the request ID before calling and replays it: the same values if current, else 410 and a new request. The unused token is revoked by the next issue. |
| Control restart | Startup revokes; the runtime exits; a replay gets 410; the manager reconnects (KRB-17). |
| Replay or second caller | Only the latest request replays. Only the manager Pod reaches 8444. |
| Orphan runtime still connected | Step 3 revokes it; step 4 waits for the drain. |

## Durable state (manager PVC)

- KRB-9. **Its own small RWO PVC** (a storage class attachable from any node the
  manager may use), at `/var/lib/sandbox-science/runtime`, mode 700, from
  `initialize-state --component runtime-manager`. Records, atomic and mode 600:
  `sandbox.json` (name, ID or null, image digest and revision, driver-config digest;
  today's `{name, id}` keys, so `recorded()` stays compatible);
  `infrastructure-recovery.json` (`RecoveryState`); `credentials.json` (the in-flight
  request ID); `request.json` and `result.json` for operator requests; `policy.yaml`;
  `runtime.json`. The runtime token is held in memory only until uploaded.

## Process model: exec session, not main process

- KRB-10. The sandbox's main process stays `sleep infinity`; the manager holds one
  `openshell sandbox exec -n N --no-tty --no-login-shell -- python -m
  sandbox_science.orchestrator.confinement.runtime --settings /sandbox/runtime/runtime.json`.
  The runtime as main process was rejected: it would start before its files can be
  uploaded; its exit after control loss would be restarted by kubelet outside the
  journal, or end the sandbox; and it would leave the qualified host path.
- KRB-11. **Fencing does not rely on the exec stream (Q5).** If the CLI's stream closes
  while the gateway is up, the gateway closes the SSH channel
  (`crates/openshell-server/src/grpc/sandbox.rs` ~3812-3829) and the supervisor
  SIGKILLs the exec process (`crates/openshell-supervisor-process/src/ssh.rs`
  `channel_close` ~495-520; `crates/openshell-sandbox-backend/src/runtime.rs`
  ~541-545). After an abrupt loss (gateway restart, relay loss) no channel close
  arrives, the handler's `Drop` (`ssh.rs` ~431-447) does not terminate exec processes,
  and the runtime can survive as an orphan. So every start revokes first (KRB-7 step 3)
  and waits for the drain; live acceptance qualifies both cases.

## Sequences

The host steps keep their order; placement, storage, credentials and trigger change.

- KRB-12. **Bootstrap (first create)**, only by the operator: `kubectl exec
  deploy/runtime-manager -- sandbox-science-runtime-manager request bootstrap`. It
  requires that no `sandbox.json` exists and resets the journal. An empty PVC, failed
  handshake or missing record never creates a runtime. Steps: write `policy.yaml` from
  `compile_orchestrator`; choose `ss-orch-<10hex>`, refusing one in use; record
  `{name, id: null}`; `sandbox create --from <digest> --policy … --driver-config-json …
  --no-auto-providers --detach -- sleep infinity`; record the first observed ID before
  checking the phase; wait for Ready, refusing dead phases; then Attach.
- KRB-13. **Attach (start into the recorded sandbox).** Precondition: the recorded
  name reports the recorded ID and is Ready. Journal a request ID; get generation and
  token (KRB-7), retrying 409 within the stop timeout, else refuse and retry later;
  derive `runtime.json` (KRB-19); upload `runtime.token`, `model.key`,
  `control-ca.crt` and `runtime.json`, `chmod 600` the credentials; run the exec.
  Ready is the "confined agent runtime ready" line plus `runtime_ready` in `/health`.
  Attach never deletes or creates, so it is not a replacement.
- KRB-14. **Replace**, in `orchestrator.replace` order: stop the exec, identity-checked
  and confirmed `delete_recorded`, the drain wait, Bootstrap's create step, Attach.
  Only on `request replace` or bounded recovery (KRB-17). Uncertain deletion keeps the
  record and refuses.
- KRB-15. **Stop.** SIGTERM ends the exec session and exits without deleting: Pod
  termination is not teardown. Teardown is `request stop --delete`: identity-checked
  deletion, removing the record once confirmed; a later start needs a bootstrap.

## Recovery and its bounds

- KRB-16. **One journal for every automatic action.** The existing `RecoveryState`:
  at most 10 attempts, delays of 2 to 60 s, reset after 60 s of steady health, pending
  turns into blocked at startup. Every automatic Attach or Replace is saved as pending
  before any effect. Explicit operator requests reset it, as `_manage` does today.
- KRB-17. **Without the operator.** Reconnect (Attach to the same identity-checked
  sandbox) is automatic at manager start and after a control restart, seen as a new
  `control_instance` (a random per-process value added to `/health`). A runtime exit
  under the same control instance (crash or revocation) is reattached only with
  `recover_infrastructure`, as on the host. Replace needs `recover_infrastructure` and
  a confirmed `delete_recorded`, never just a probe failure or restart. An unknown or
  changed ID, a pending journal or undeterminable presence blocks all automatic action
  and shows in status.

| Event | Behavior |
| --- | --- |
| App Pod restart or control crash | Control revokes at start; the runtime exits; the manager reconnects. |
| Manager Pod restart | The CLI dies with it, which normally kills the runtime (KRB-11). The manager reconnects, revoking any survivor first. Nothing is deleted. |
| Runtime node lost | Not Ready, not confirmed gone: deletion cannot be confirmed (the Pod stays Terminating), so the record stays and the attempt counts. Force-deleting is an operator action. |
| Manager node lost | Rescheduled once its RWO volume detaches; then as a manager restart. |
| Gateway unavailable | No ownership change; a transport error is never absence. The runtime may survive (KRB-11). Attempts consume the budget, then block. |
| Manager PVC lost | No record, so nothing is created; the operator reconciles any old sandbox. |

## Control and Secret projection

- KRB-18. **Control.** `control.json` gains `manager_listen` and
  `manager_token_sha256_file`; token and generation paths follow KRB-6; `/health`
  gains `control_instance`. `control-token` leaves the control Secret's
  `REQUIRED_FILES`, which gains `runtime-manager-token.sha256`.
- KRB-19. **Runtime settings.** The manager Secret carries the non-secret model part
  of `control.json` (aliases, APIs, efforts, embedding, image revision), from which the
  manager derives `runtime.json` as `prepare()` does, plus the issued generation. Its
  settings supply `control_url` (Service, 8443), `manager_url` (8444),
  `control_ca_file`, `model_url`, `thinking_replay` and the runtime image digest and
  revision. It refuses a revision differing from the copy's (control's handshake also
  refuses one differing from control's) and a `control_url` off the control Service.

## Renderer changes

- KRB-20. `kubernetes_config` gains `images.runtime` (digest-pinned);
  `secrets.runtime_manager` (`runtime-manager.json`, `manager-control.token`,
  `model.key`, `control-ca.crt`, `gateway-ca.crt`, `gateway-tls.crt`,
  `gateway-tls.key`, `gateway-metadata.json`); a `runtime` section (KubernetesProfile
  fields with Kata required, gateway workspace and name, `recover_infrastructure`);
  `network.gateway` (manager only); capacity and storage for `runtime_manager`.
- KRB-21. `kubernetes_bundle` gains the manager Deployment (KRB-1; no probes, which
  give no replacement authority; uid 1000, read-only root), its PVC, copy init (nested
  destinations, which `copy_credentials.py` accepts) and `initialize-state` init;
  control's `runtime-credentials` memory volume and port 8444 on container and
  Service; the KRB-4 NetworkPolicies; and per-container app PVC subPath mounts so
  workspace never mounts `control/`. Output stays deterministic; the renderer never
  reads Secrets or a kubeconfig.

## Reuse and new code

- KRB-22. **Extract `sandbox_science.launcher.runtime_owner`** for the host launcher
  and the manager: `recorded`, `_record`, `delete_recorded` over an `OwnershipStore`
  and a `SandboxCli` protocol; `_agent`'s create, Ready and ID part as
  `create_owned(...)` with optional driver JSON; its upload part as `install(...)` and
  `exec_argv(...)`; `agent_settings(...)` from `prepare`; `_occupied` with TLS, CA and
  hostname. `InfrastructureRecovery` takes a liveness predicate, a replace coroutine
  and a journal path instead of a `Supervisor`; `replacement` receipts bind to the
  manager's instance nonce. Host behavior and tests are unchanged.
- KRB-23. **Move and fix.** `KubernetesProfile` and `driver_json` move to
  `sandbox_science.policy` (the worker re-exports them). `OpenShell.sandbox()` reports
  absence only for a successful lookup saying "no such sandbox" (checked against the
  0.1.5 CLI), never for stderr containing "not found", as `launcher/mcp.py` and
  `worker/environments.py` do today.
- KRB-24. **New code.** Orchestrator: `confinement/credentials.py` (journal, mint,
  revoke at start, the KRB-7 states) and the second listener in `server.py`. Launcher:
  `sandbox_science.launcher.runtime_manager` (`serve`: reconcile, reconnect, watch the
  exec, recover; `request bootstrap|replace|stop|status`), `RuntimeManagerSettings`, a
  credentials client and the `sandbox-science-runtime-manager` script. `trusted-app`
  gains the launcher and policy wheels and the OpenShell CLI at the gateway's release.

## Delivery and tests

Stages, each merged green: (1) `runtime_owner` extraction and the absence fix; (2) the
control endpoint and listener, off unless configured; (3) the manager with the fake
CLI; (4) renderer and image; (5) live acceptance.

- KRB-25. **Unit tests.** Control: digest auth and 404; replay of the latest request;
  410 for older and superseded; revoke before 409 while connected or draining; a crash
  between token and journal yielding fresh values; revoke at start; no reissued
  generation; the runtime listener lacking the endpoint. Manager, with
  `tests/fake_openshell.py` extended (exec streams, upload, driver config, gateway
  errors containing "not found"): bootstrap refused without a request, on an empty PVC
  or with a record; record before create, ID before Ready; uncertain create and delete
  keeping the record; name or ID mismatch and non-Kata profiles refused; request ID
  replay after a crash; reconnect at start and on a new control instance only;
  blocked-at-startup and the 10-attempt bound; Replace only with opt-in and confirmed
  absence; SIGTERM not deleting.
- KRB-26. **Process test:** real TLS control with both listeners, the real manager and
  the fake CLI, whose exec runs the real runtime against a fake model: handshake,
  orphan revocation, manager restart, control restart and explicit replace keep
  control's history. Renderer tests: manager Deployment, PVC, Secret and policies; no
  `control-token`; only the manager mounts the gateway profile; workspace lacks
  `control/`.
- KRB-27. **Live acceptance on Kata (amd64), disposable:** the workload Pod admitted
  under Kata; no agent loop in a trusted Pod; the app Pod unable to read the gateway
  profile; the runtime's supervisor reaching 8443 and a research sandbox refused by
  its own policy; 8444 reachable only from the manager; denied egress still denied;
  app Pod and manager Pod deletion each followed by reconnect to the same sandbox ID;
  whether a gateway restart orphans the runtime, and revocation ending it; explicit
  replace, then a user Resume while a child computation continues in its surviving
  workload sandbox (RC-F20); confirmed teardown. Fixtures do not count.

## Blockers in existing code

- Trusted images pin OpenShell CLI 0.1.3, and `driver_json` targets its schema.
- `trusted-app` has no OpenShell CLI and no launcher or policy wheels.
- Absence by the string "not found" (KRB-23) could erase a record in a gateway outage.
- The renderer mounts the whole app PVC into workspace and requires `control-token`.
- `OrchestratorTarget.worker_api` is required though the runtime never calls the
  worker; making it optional is cleaner.

## Decisions (2026-10-22)

1. The manager runs in its own Pod with its own PVC and holds the only gateway
   credential outside the worker; control issues runtime credentials (KRB-1, KRB-6).
2. The agent runtime always runs under Kata (tier 3) on amd64 (KRB-5).
3. The first start is an operator command through `kubectl exec`; on-demand starts
   are a later direction (KRB-12).
4. The manager reconnects automatically to the same recorded sandbox; replacing it
   still needs `recover_infrastructure` and the 10-attempt bound (KRB-17).

Remaining owner questions:

- R1. The manager's gateway certificate can do anything on the gateway, including to
  the worker's sandboxes. OpenShell can confine it to one workspace only if the gateway
  turns on OIDC roles (for example through Authentik), which also changes how the
  worker and you sign in. Recommendation: accept for the first deployment and put
  gateway roles on the backlog.
- R2. Kubernetes cannot tell the runtime's traffic from any other sandbox's on this
  gateway, so every sandbox can reach control's runtime port; each sandbox's own
  OpenShell rules and the runtime token still stop them. The alternative is a separate
  gateway for the runtime. Recommendation: accept, and prove the refusal live.
