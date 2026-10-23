# Kubernetes runtime bootstrap

Status: Draft design (2026-10-22); not built. It closes the gate in
[the offline bundle](../KUBERNETES-BUNDLE.md#render-inspect-then-qualify-separately)
that says "cluster runtime bootstrap is an unimplemented gate". It applies
[orchestrator confinement](orchestrator-confinement.md#launcher-and-replacement),
[Kubernetes operations](kubernetes-operations.md#first-deployment-bundle) and
[research continuity](research-continuity.md#orchestrator-boundary) (RC-26 to RC-29,
RC-F20) to the cluster. Requirements are numbered KRB-n.

## Context

Every agent and model loop runs in one OpenShell sandbox from the `orchestrator`
image. On babbage the host launcher (`launcher/orchestrator.py`) writes the policy, a
fresh `runtime-<24hex>` generation, a rotated control token, `control.json` and
`runtime.json`; records the name before create and the immutable ID before Ready;
uploads token, model key and settings to `/sandbox/runtime`; and holds an `openshell
sandbox exec` session running the runtime. Explicit replacement (`replacement.py`) and
bounded recovery (`infrastructure.py`) reuse the identity-checked `delete_recorded`.
Nothing does this in the rendered bundle. Control is already cluster-ready: TLS,
optional CA, and token and generation files reread on every frame and at handshake
(`confinement/server.py`). The runtime makes one control connection, exits when it
ends, and has its generation fixed in `runtime.json` (`runtime.py`): one process
serves one connection with one generation.

## Non-goals

No agent loop, runtime CLI or model call in a trusted Pod. No research or MCP sandbox
lifecycle (that stays with the worker). No multiple runtimes, app replicas or
failover; no public ingress, image publication, ARM64 Kata or operator/CRD; no change
to the control protocol, generation fencing or attempt model.

## Placement and authority

- KRB-1. **A `runtime-manager` container in the app Pod.** It runs next to control and
  workspace. It is the only process that creates, attaches to, replaces or deletes the
  runtime sandbox. It runs no agent loop or model call, and serves no network listener.
  Why the app Pod and not the worker pool Pod, which already holds a gateway identity:
  token and generation rotation must happen beside control's files, and the drain check
  reads control's `/health`. A pool-side manager would need a new authenticated
  rotation endpoint on control, which is a larger new attack surface.
- KRB-2. **Its own gateway identity.** The manager's Secret holds an OpenShell CLI
  profile with its own mTLS client certificate, distinct from the worker's so it can be
  revoked separately, plus the model key, the control CA and `runtime-manager.json`.
  The copy init builds the CLI's XDG tree (`openshell/active_gateway`,
  `gateways/<name>/metadata.json`, `mtls/{ca.crt,tls.crt,tls.key}`) in a memory volume
  only the manager mounts; workspace and control never mount its Secret, volume or
  HOME, and `shareProcessNamespace` stays false. This amends the bundle doc's rule that
  gateway credentials belong only to the worker, which needs the owner's approval (Q1).
- KRB-3. **The manager acts only on its own record:** a recorded name matching
  `ss-orch-[0-9a-f]{10}` for which the gateway reports the recorded ID. A gateway mTLS
  identity is broad (Q2), so this is a guard against a corrupt or planted record, not
  an authorization boundary.
- KRB-4. **Network.** A new required `network.gateway` egress (peer and ports) is
  rendered for the app Pod when the manager is enabled. NetworkPolicy is per Pod, so
  workspace shares this flow. The gateway's mTLS still authenticates every call.
  Runtime-to-control stays `network.runtimes` to TCP 8443 only. Those peers should
  match only the runtime sandbox's Pod, not every OpenShell sandbox (Q3). The manager
  reads control's `/health` over Pod loopback with TLS, the control CA and the Service
  hostname. No new ingress is added.
- KRB-5. **Kata placement.** Create with `--driver-config-json` from the operator's
  runtime profile: an `amd64` node selector, the Kata `runtime_class_name` and
  resources. The gateway enables `allow_driver_config`; the RuntimeClass, shared
  infrastructure to OpenShell 0.1.5's admission, carries
  `openshell.ai/sandbox-attachable=true` ([gateway settings](../KUBERNETES.md#gateway-settings-for-profile-placement)).
  The renderer refuses a runtime
  profile without a RuntimeClass (tier 3 only; Q4). The exact driver JSON is recorded
  with ownership; a replacement reuses it unless an explicit request names another.

## Durable state

- KRB-6. **PVC layout.** `initialize-state --component app` also creates
  `/var/lib/sandbox-science/runtime` (mode 700). It is mounted by subPath into the
  manager only. Control and workspace mount the `control` subPath and their own home
  and config subPaths, never the whole PVC. All containers run as uid 1000, so file
  modes cannot separate them. Only the mounts do. A workspace that could write the
  ownership record could aim the manager's deletion at another sandbox.
- KRB-7. **Records in `runtime/`**, each written atomically with mode 600:
  `sandbox.json` (name, ID or null, image digest, image revision, driver-config digest);
  `infrastructure-recovery.json` (the existing `RecoveryState`); `request.json` and
  `result.json` for operator requests; `policy.yaml`; and `runtime.json`. The policy and
  runtime settings are rewritten on each start. `sandbox.json` keeps today's
  `{name, id}` keys, so `recorded()` stays compatible.
- KRB-8. **Rotating capability files stay off the PVC.** `runtime.token` and
  `generation` live in a memory `emptyDir` named `runtime-handoff`. The manager mounts it
  read-write and control mounts it read-only. It is never mounted into workspace. Mount
  the directory, not single files by subPath, so that an atomic rename reaches control.
  Losing them with the Pod is intended: the next start rotates anyway, and a runtime
  from before the restart cannot reconnect.

## Process model: exec session, not main process

- KRB-9. The sandbox's main process stays `sleep infinity`. The manager holds one
  `openshell sandbox exec -n N --no-tty --no-login-shell -- python -m
  sandbox_science.orchestrator.confinement.runtime --settings /sandbox/runtime/runtime.json`.
  Running the runtime as the main process was rejected: (1) it would start before
  its token, key and settings can be uploaded (upload needs a Ready sandbox), so the
  runtime would need a new wait-for-files mode; (2) its exit after any control loss
  would end the main process, and kubelet would restart it outside the manager's
  journal with stale files or none (uploads sit in the writable layer), or the
  sandbox would go to a terminal phase only replacement can fix; (3) it would break
  reuse of the host path that the qualification verified.
- KRB-10. **Fencing does not depend on the exec stream.** Whether a dropped exec
  stream (manager crash, gateway restart) kills the remote process is unqualified on
  0.1.5 (Q5). So every start first rotates generation and token; control rereads the
  token on every frame, revoking an orphan at its next poll, and the manager waits for
  the drain before starting the next runtime.

## Sequences

The host steps keep their order. Only placement, storage and the trigger change.

- KRB-11. **Bootstrap (first create).** It runs only after an explicit operator
  request (`sandbox-science-runtime-manager request bootstrap`, through `kubectl exec`
  into the manager container). It requires that no `sandbox.json` exists and, being
  explicit, resets the journal. An empty PVC, a failed handshake or a missing record
  alone never creates a runtime. Steps: write `policy.yaml` from `compile_orchestrator`; choose
  `ss-orch-<10hex>` and refuse if it is in use; record `{name, id: null}`; run
  `sandbox create --from <digest> --policy … --driver-config-json …
  --no-auto-providers --detach -- sleep infinity`; record the first observed ID
  before checking the phase; wait for Ready, refusing dead phases; then Attach.
- KRB-12. **Attach (start into the original sandbox).** Precondition: the recorded
  name reports the recorded ID and is Ready. Steps: rotate (generation, then token,
  into `runtime-handoff`); wait within the stop timeout until control's `/health`
  shows `runtime_connected` and `runtime_draining` both false, else refuse and retry
  later; derive `runtime.json` (KRB-17); `mkdir`, upload `runtime.token`,
  `model.key`, `control-ca.crt` and `runtime.json`, and `chmod 600` the credentials;
  run the exec session. Readiness is the "confined agent runtime ready" line plus
  control's handshake. Attach never deletes or creates a sandbox. It keeps the
  recorded original identity, so it is not a replacement and runs at manager start.
- KRB-13. **Replace.** This is the existing `orchestrator.replace` order: stop the exec
  session, then `delete_recorded`, which is identity-checked and must confirm the
  deletion; then the control-drain wait; then Bootstrap's create step and Attach. It
  happens only on an explicit operator `request replace`, or as bounded automatic
  recovery (KRB-16). Uncertain deletion keeps the record and refuses.
- KRB-14. **Stop.** On SIGTERM, the manager ends its exec session and exits. It does not
  delete the sandbox, unlike the host's `on_stop`, because Pod termination is not
  teardown. Teardown is an explicit operator `request stop --delete`: identity-checked
  deletion, removing the record only once confirmed; a later start needs a bootstrap.

## Recovery and its bounds

- KRB-15. **One journal for every automatic action.** The existing `RecoveryState`
  gates automatic actions: at most 10 attempts, delays of 2 to 60 s, reset after 60 s of
  steady health, and pending turns into blocked at startup. Every automatic Attach or
  Replace is saved as pending before it has any effect. Explicit operator requests
  reset the journal, as `_manage` does today.
- KRB-16. **What may happen without the operator.** At manager start: Attach, if the
  original is present and Ready. When the runtime exits while the manager runs:
  Attach only with `recover_infrastructure`, mirroring the host, where a dead
  `agent-runtime` otherwise stays down. Replace only with `recover_infrastructure`
  and after an identity-checked `delete_recorded` confirmed the original is gone; the
  permission comes from the owner's standing opt-in plus confirmed absence, never
  from a probe failure or a restart. An unknown or changed ID, a pending journal, or
  undeterminable presence blocks all automatic action and shows in status.

### Failure cases

| Event | Behavior |
| --- | --- |
| App Pod restart | Control and the handoff files are new; the old runtime's connection ends and it exits. A pending journal becomes blocked; otherwise the manager Attaches to the original. |
| Manager container crash | Kubelet restarts the manager alone. The exec stream is gone and the runtime may be orphaned. Startup rotation revokes it, and the manager Attaches after the drain. Nothing is deleted. |
| Control crash | The runtime's connection ends and it exits. The manager restarts it only under KRB-16. |
| Runtime node lost | The sandbox shows not Ready but is not confirmed gone. Deletion cannot be confirmed (the Pod stays Terminating), so the record is retained and the attempt counts. Force-deleting the Pod is an operator action. |
| App node lost | The app Pod is rescheduled after the RWO volume detaches. Then it follows the App Pod restart row. |
| Gateway unavailable | No ownership change. A transport error is never read as absence. The exec stream drops (see Q5). Automatic attempts consume the budget and then block. |
| PVC lost or replaced | With no record, nothing is created. The old sandbox, if it still exists, is found and reconciled by the operator. |

## Control and Secret projection

- KRB-17. **Control settings.** `control.json` sets `runtime_token_file` and
  `generation_file` to the `runtime-handoff` paths. Its static `generation` becomes a
  fixed placeholder that no `runtime.json` carries. `control-token` leaves the control
  Secret's `REQUIRED_FILES`. The manager reads `control.json`, projected read-only
  from the control Secret, and from it derives `runtime.json`: model aliases, APIs,
  efforts, embedding and generation, as `prepare()` does on the host. Its own settings
  supply `control_url` (the HTTPS Service URL), `control_ca_file`, `model_url`,
  `thinking_replay` and the runtime image digest and revision. The manager refuses to
  start if the revision differs from `control.json.image_revision`. A runtime
  `control_url` that is not on the control Service is also refused.

## Renderer changes

- KRB-18. `kubernetes_config` gains: `images.runtime` (digest-pinned);
  `secrets.runtime_manager`, whose required files are `runtime-manager.json`,
  `model.key`, `control-ca.crt`, `gateway-ca.crt`, `gateway-tls.crt`,
  `gateway-tls.key` and `gateway-metadata.json`; a `runtime` section with the
  KubernetesProfile fields, gateway workspace and name, and `recover_infrastructure`;
  `network.gateway`; and capacity for `runtime_manager`.
- KRB-19. `kubernetes_bundle` gains: the manager container (no probes, which give no
  replacement authority) and its copy init with nested destination paths, which
  `copy_credentials.py` already accepts; the `runtime-handoff` memory volume;
  per-container PVC subPath mounts (KRB-6) and the `runtime` directory in
  `initialize_state.py`; and gateway egress for the app Pod. Output stays
  deterministic, and the renderer still never reads Secrets or a kubeconfig.

## Reuse and new code

- KRB-20. Extract into `sandbox_science.launcher.runtime_owner`, used by the host
  launcher and by the manager: `recorded`, `_record` and `delete_recorded`, over an
  `OwnershipStore` (a directory) and a `SandboxCli` protocol; the create, Ready and
  ID part of `_agent` as `create_owned(...)`, taking optional driver JSON; its upload
  part as `install(cli, name, files)` and `exec_argv(...)`; the rotation part of
  `prepare` as `rotate(generation_path, token_path)` and `agent_settings(...)`; and
  `_occupied`, with TLS, CA and hostname. `InfrastructureRecovery` takes a liveness
  predicate, a replace coroutine and a journal path instead of a `Supervisor`. The
  `replacement.Request`/`Result` receipts are reused, bound to the manager's instance
  nonce instead of pid/start/boot. Host behavior is unchanged and its tests pass as is.
- KRB-21. **Move code into `sandbox_science.policy`.** `KubernetesProfile` and
  `driver_json` move there from `worker/kubernetes.py`, and the worker re-exports them,
  so the manager does not need the worker package. `OpenShell.sandbox()` must report
  absence only for a successful lookup that says "no such sandbox" (exit status or
  structured output, checked against the 0.1.5 CLI). It must not treat any stderr
  containing "not found" as absence, which `launcher/mcp.py` and
  `worker/environments.py` both do today.
- KRB-22. **New code:** `sandbox_science.launcher.runtime_manager` with `serve`
  (reconcile, Attach, watch the exec, recover) and `request
  bootstrap|replace|stop|status`; `RuntimeManagerSettings`; the
  `sandbox-science-runtime-manager` script. The `trusted-app` image gains the
  launcher and policy wheels and the OpenShell CLI pinned at the gateway's release.

## Tests and acceptance

- KRB-23. **Unit tests** with `tests/fake_openshell.py`, extended with exec streams,
  upload and driver-config recording, and gateway-unreachable errors that contain
  "not found". Cover: bootstrap refused without a request, on an empty PVC or with an
  existing record; record-before-create and ID-before-Ready; uncertain create and
  delete keeping the record; name-pattern and ID mismatch refused; rotation before
  every start and the drain wait; pending turning into blocked at startup and the
  10-attempt bound; Replace only with opt-in and confirmed absence; gateway errors
  never meaning absence; SIGTERM not deleting.
- KRB-24. **Process test:** a real TLS control server and the real manager with the
  fake CLI, whose exec runs the real runtime module against a fake model. Prove that
  handshake, rotation revoking an orphan, manager restart, control restart and
  explicit replace keep control's history. Renderer tests cover the mounts, the
  absence of `control-token`, and that only the manager mounts the gateway profile.
- KRB-25. **Live acceptance on Kata (amd64), disposable:** the runtime Pod admitted
  under the Kata RuntimeClass; no agent loop in the app Pod; workspace unable to read
  the gateway profile or handoff files; denied egress still denied; App Pod deletion
  followed by Attach to the same sandbox ID; manager kill and gateway restart within
  bounds; explicit replace, then a user Resume of the original tree while a child
  computation continues in its surviving workload sandbox (RC-F20); confirmed
  teardown. Fixtures do not count as this evidence.

## Blockers in existing code

- The trusted images pin OpenShell CLI 0.1.3. The isolation plan assumes 0.1.5 and
  `allow_driver_config`. `KubernetesProfile.driver_json` targets the 0.1.3 schema.
- `trusted-app` has no OpenShell CLI and no launcher or policy wheels.
- Absence detection by the string "not found" (KRB-21) could erase an ownership record
  during a gateway outage.
- The renderer mounts the whole app PVC into workspace and requires a static
  `control-token`.
- `OrchestratorTarget.worker_api` is required though the runtime never calls the
  worker; the cluster policy would name the worker Service (NetworkPolicy denies it).
  Making it optional is cleaner.

## Open questions for the owner

1. Accept a gateway client credential in the app Pod (and amend the bundle doc), or
   keep it in the pool Pod and add a rotation endpoint to control?
2. Does an OpenShell 0.1.5 mTLS client identity have full gateway authority? If so, is
   a separate manager identity enough, or is a scoped gateway role required first?
3. Can driver config set Pod labels, so that `network.runtimes` admits only the
   runtime's Pod rather than all sandboxes?
4. Must the runtime always be on tier 3 (Kata), or may an operator choose tier 1?
5. Does a dropped exec stream, or a gateway restart, terminate the remote runtime in
   0.1.5? This needs qualifying before relying on it.
6. Is `kubectl exec` into the manager acceptable as the operator request channel?
7. Should Attach at manager start also require `recover_infrastructure`, which would
   make every app Pod restart wait for the operator?
