# Offline Kubernetes deployment bundle

The `sandbox-science-kubernetes check|render` command prepares a deterministic JSON
Kubernetes List. It reads only its explicit, bounded site-private TOML file. It does
not read kubeconfig or referenced Secrets, call `kubectl`, apply resources, build or
publish images, or create an OpenShell runtime. No actual cluster or native ARM64
deployment is qualified by rendering or schema validation.

The trusted application image runs control and workspace in one Pod over one block
PVC; each container mounts only its own directories of it (`control/`, which the
workspace also reads as the harness's state, and its managed home/config
directories). A second Pod runs worker and policy middleware over their shared block
PVC; package cache is optional there. A third Pod, the
[runtime manager](specs/kubernetes-runtime-bootstrap.md), runs the same
`trusted-app` image as `sandbox-science-runtime-manager serve` over a small PVC of its
own. It alone holds an OpenShell gateway credential outside the worker, and it alone
creates, attaches to, replaces or deletes the agent runtime's Kata sandbox; it has no
Service, listener or probes and runs no agent loop or model call. Deployments use one
replica and Recreate strategy,
retain PVCs independently of Deployment ownership, and rely on the existing service
state locks. All containers run as UID/GID 1000 with fsGroup 1000, no added Linux
capabilities, no privilege escalation, a read-only root filesystem and default
seccomp. No application service account token, RBAC, privileged Pod, host path,
runtime agent loop or research process is generated. Install and qualify OpenShell
separately with its own required gateway authority and isolation tier. The gateway
needs `allow_driver_config = true` with resource admission on, and each RuntimeClass
and PVC a profile names must carry the `openshell.ai/sandbox-attachable` labels
([gateway settings](KUBERNETES.md#gateway-settings-for-profile-placement)).

A first UID-1000 init container mounts only that Pod's data PVC and creates the
fixed missing state directories: `control` for the application, `worker` and
`middleware` for the pool, plus `package-cache` when enabled, and `runtime` for the
runtime manager. This breaks the cold
volume startup dependency: workspace can initialize its empty control database
before any runtime has connected. Initialization is create-only, preserves existing
directory modes and all content, refuses symlink/non-directory collisions and does
not reset databases, recursive permissions, policies, ownership or runtime state.
The PVC mount root must already be writable through the configured fsGroup; this
helper does not repair volume-driver ownership or substitute for operator bootstrap.

## Prepare private configuration

Copy [the template](../examples/kubernetes-bundle.toml) outside Git. Set `images.app`
and `images.worker` to approved registry URLs pinned with their exact nonzero
SHA-256 digests, using the `trusted-app` and `trusted-worker` build recipes. Published
older images do not contain this increment; publication is deferred by the owner.
The template deliberately omits runnable pins. No renderer option resolves a tag,
downloads an image or substitutes a placeholder. Match both image platform coverage
and the explicit `app.architecture` / `pool.architecture` (`amd64` or `arm64`).

Set an existing namespace, bundle name, each `storage_gib` and optional
`storage_class`. Use block-backed SQLite-compatible storage; RWX/NFS does not make
independent SQLite writers safe. An omitted class uses the cluster default. The
renderer cannot verify its type, binding behavior, capacity or reclaim policy.
Inspect the actual StorageClass/PV and choose Retain behavior before real data.
Do not delete the generated PVCs or run `kubectl delete -f` on the entire bundle as
ordinary shutdown. A PVC is not a backup and moving artifacts to S3 does not archive
the complete workspace or metadata recovery set.

Each of `control`, `workspace`, `worker`, `middleware`, optional `cache` and
`runtime_manager` accepts these integer bounds; missing fields use the defaults below. Requests must fit
limits and scratch must fit the ephemeral storage limit. These are per-container
trusted-service resources, independent of OpenShell research profile resources.

| Field | Default | Units |
| --- | ---: | --- |
| `cpu_request_millis`, `cpu_limit_millis` | 250, 1000 | millicores |
| `memory_request_mib`, `memory_limit_mib` | 256, 1024 | MiB |
| `ephemeral_request_mib`, `ephemeral_limit_mib` | 128, 1024 | MiB |
| `scratch_mib` | 128 | MiB for that container's `/tmp` emptyDir |

Tune these to the measured workload and node capacity. The kernel enforces resource
limits; a request alone reserves scheduler capacity. Service data must use the PVC
layout below; the image root and credential mounts are read-only. `/tmp` is scratch,
not retained state. Credentials have a separate 4 MiB memory emptyDir per service.

`[runtime_manager]` also takes the manager Pod's `architecture` (`amd64` or `arm64`;
the `trusted-app` image must cover it), `storage_gib` and optional `storage_class`
for its own `ReadWriteOncePod` PVC (attachable from any node of that architecture;
the class's CSI driver must support that access mode, so a second manager Pod, even on
the same node, can never mount its state), and
`openshell_gateway`, the gateway name of its OpenShell CLI profile. Its `/tmp` is a
memory volume, since the runtime token passes through a private file there before
upload, so its `scratch_mib` must also fit its memory limit. It accepts no
`environment` table.

## Configure the workspace model catalog

`models.main` and `models.delegated` are required. Each selects a nonsecret `alias`,
`api` (`responses`, `chat` or `messages`) and optional `effort`; the optional
`models.reviewer` selects the reviewer role. These become the workspace CLI's
`--agent-model`, `--agent-api` and `--agent-effort` options, enabling the project
model settings GUI instead of leaving it unavailable. The default API is Responses.
Non-Messages research roles default to `max` effort; a configured non-Messages
reviewer defaults to `medium`. Set its alias to the site's approved cheaper reviewer
model. Messages accepts no requested effort, including an explicit effort in TOML.
One shared alias must have the same API in every role, though effort is per role.

The example names generic research/reviewer aliases, not real gateway routes. This
catalog contains no model endpoint, API key or credential and makes no LLM calls.
Keep the control settings and separately installed runtime's model allowlists,
adapters and effort defaults aligned with this catalog. The renderer cannot inspect
the private `control.json` or verify gateway availability. Configuring the GUI does
not itself start the runtime or enable math-repair model calls in the workspace.

## Prepare existing Secrets and recovery state

The operator creates the named Secrets through the site's existing secure workflow.
The renderer generates no Secret contents, tokens, settings or model credentials.
Each service receives a different private memory volume mounted at `/private`.
Only its init container mounts that service's projected Secret at `/source`; the
narrow image helper copies selected files to regular mode-600 files owned by UID
1000. Main containers cannot mount the Secret projection or another service's
private credential volume. Identical init retries preserve files, while changed
credentials require an intentional Pod recreation. Existing credential checks stay
enabled. Kubernetes still exposes the original Secret to administrators with the
corresponding cluster authority; this is not a replacement for cluster Secret policy.

| Secret setting | Required keys copied to `/private/<key>` |
| --- | --- |
| `secrets.control` | `control.json`, `runtime-manager-token.sha256`, `worker-token`, `control.crt`, `control.key`, `worker-ca.crt` |
| `secrets.workspace` | `worker-token`, `worker-ca.crt` |
| `secrets.worker` | `worker.toml`, `worker.crt`, `worker.key` |
| `secrets.middleware` | `middleware.toml`, `middleware.crt`, `middleware.key` |
| optional `secrets.cache` | `cache.toml` |
| `secrets.runtime_manager` | `runtime-manager.json`, `control-models.json`, `manager-control.token`, `model.key`, `control-ca.crt`, and the OpenShell CLI profile `active-gateway`, `gateway-metadata.json`, `gateway-ca.crt`, `gateway-tls.crt`, `gateway-tls.key` |

The runtime manager's Secret is its own (no other service may name it) and takes no
`extra_files`. No other service's `extra_files` may name `manager-control.token`,
`model.key` or a gateway profile key. Its copy init places the profile keys as the
pinned CLI's tree under `/private/xdg`: `openshell/active_gateway` (the key
`active-gateway`, holding exactly the `openshell_gateway` name),
`openshell/gateways/<openshell_gateway>/metadata.json` and that directory's
`mtls/ca.crt`, `mtls/tls.crt` and `mtls/tls.key`. Every other key keeps its name
under `/private`. The profile's client certificate is the manager's own, issued by the
gateway's client CA; without OpenShell OIDC roles it has platform-admin authority on
the gateway, and revoking it means replacing the client CA
([R1](specs/kubernetes-runtime-bootstrap.md#decisions-2026-10-22)).

`name` selects the pre-existing Secret. `extra_files` may list up to 24 additional
unique leaf keys (up to 64 characters, letters/digits/dot/dash/underscore), such as
pool tokens, policy ceiling, targets, gateway CLI configuration, public verification
keys, S3 configuration or a standard credential-chain file. Required keys are always
copied. Every listed key must exist; unrelated Secret keys are not projected. Keep
all absolute paths in private settings aligned with `/private` and the PVC layout.
Do not put token or key values in TOML, shell arguments or the rendered JSON.

Each service's `environment` table can select standard SDK discovery paths. File
values for `AWS_SHARED_CREDENTIALS_FILE`, `AWS_CONFIG_FILE` and `AWS_CA_BUNDLE` must
be exact `/private/<key>` paths projected for that same service. `AWS_PROFILE` and
`AWS_REGION` accept bounded names; explicit application S3 settings still determine
the configured endpoint/region. Metadata credentials remain disabled through
`AWS_EC2_METADATA_DISABLED = "true"`, which cannot be changed to false. No inline
access-key/session-token variables, arbitrary process/code-path environment,
proxy configuration or TLS-verification bypass is accepted.

For example, grant the workspace a separate read-only object-store principal and
create its private credential/config keys through the site's Secret workflow:

```toml
[secrets.workspace]
name = "workbench-workspace"
extra_files = ["s3-credentials", "s3-config"]

[workspace.environment]
AWS_SHARED_CREDENTIALS_FILE = "/private/s3-credentials"
AWS_CONFIG_FILE = "/private/s3-config"
AWS_PROFILE = "artifact-reader"
```

Only paths and the profile name appear in the rendered manifest. The credential
file holds the actual keys; do not put them in this TOML. Select a suitable writer
profile separately under `control.environment`, and use another prefix-limited
principal for an operator's checkpoint transport. The renderer validates file
selection, not the permissions, certificate contents or SDK profile correctness.

OpenShell CLI state may need a persistent home/config tree prepared by the trusted
operator on the pool PVC. The optional `worker.environment.HOME` value is exactly
`/var/lib/sandbox-science/worker-home`; `XDG_CONFIG_HOME` is exactly
`/var/lib/sandbox-science/worker-config`. Other roles can select their corresponding
`<role>-home` / `<role>-config` paths. Prepare those owned, private directories and
the CLI's real directory structure before use; selecting a directory does not
initialize a gateway identity or populate it from flat Secret keys. A standalone
`gateway.toml` extra file is not automatically a complete OpenShell CLI profile.
The pinned CLI's XDG tree includes `openshell/active_gateway` and
`openshell/gateways/<name>/metadata.json` plus the gateway's
`mtls/ca.crt`, `mtls/tls.crt` and `mtls/tls.key`; preserve that actual tree through
the trusted operator's existing secure configuration workflow.
Default HOME is the existing per-container `/tmp` scratch volume. Engine/gateway
credentials belong only in the worker's trusted state/private settings and the
runtime manager's Secret, never in the application containers. The renderer still does not mount kubeconfig or an engine
socket or grant cluster authority to any application.

Configure the actual service settings using the existing
[worker](WORKER.md), [control architecture](ARCHITECTURE.md) and
[policy middleware](POLICY-MIDDLEWARE.md) documentation. Important cluster bindings:

* `control.json`: `state_dir` is `/var/lib/sandbox-science/control`, `listen` is
  `0.0.0.0:8443`, `tls.certificate` is `/private/control.crt`, `tls.private_key`
  is `/private/control.key`. Control mints the runtime's generation and token itself
  (KRB-6): `runtime_token_file` is
  `/run/sandbox-science/runtime-credentials/runtime.token`, a memory volume only
  control mounts; `generation_file` is
  `/var/lib/sandbox-science/control/runtime-generation`; `generation` is a placeholder
  never issued; `manager_listen` is `0.0.0.0:8444` and `manager_token_sha256_file` is
  `/private/runtime-manager-token.sha256`, the lowercase SHA-256 hex of the manager's
  bearer token. There is no static runtime token.
  `worker` is `https://<name>-worker.<namespace>.svc:8100`, `worker_ca_file` is
  `/private/worker-ca.crt` and `worker_token_file` is `/private/worker-token`.
  Record the trusted runtime image revision, permitted projects, model
  aliases/effort and other required settings; the renderer cannot infer them.
* `runtime-manager.json` ([settings](specs/kubernetes-runtime-bootstrap.md#the-manager-as-built)):
  `state_dir` is `/var/lib/sandbox-science/runtime`; `control_url` is
  `https://<name>-control.<namespace>.svc:8443` and `manager_url` the same host at
  `8444`; `control_ca_file` is `/private/control-ca.crt`, `manager_token_file`
  `/private/manager-control.token` (whose digest is control's
  `runtime-manager-token.sha256`), `control_models_file` `/private/control-models.json`
  (a copy of `control.json`, or its model part) and `model_key_file`
  `/private/model.key`. `openshell.env.XDG_CONFIG_HOME` defaults to `/private/xdg`,
  where the profile is mounted; the manager passes the CLI only `openshell.env` and
  its standard environment. `image`
  and `revision` are the `orchestrator` runtime image's digest and source revision
  (matching `control.json`'s `image_revision`), `policy` the runtime sandbox's
  OpenShell target (control's runtime Service, the model gateway) and `runtime` its
  Kata, amd64 Kubernetes profile; `recover_infrastructure` opts into bounded
  automatic replacement. The container sets
  `SANDBOX_SCIENCE_RUNTIME_MANAGER_SETTINGS=/private/runtime-manager.json`.
* `worker.toml`: `listen = "0.0.0.0:8100"`, `[tls]` certificate/private key point at
  `/private/worker.crt` and `/private/worker.key`. Use state under
  `/var/lib/sandbox-science/worker`; OpenShell policy and event stores point at
  `/var/lib/sandbox-science/middleware` on the same pool PVC. Configure the actual
  OpenShell backend, CLI configuration, ceiling, target addresses, trusted profile
  image pins and project-scoped client tokens. The control client gets only its
  approved project operations, including artifact retention; the workspace token
  remains a separate observation/cancellation principal without retention authority.
* `middleware.toml`: `listen = "0.0.0.0:50051"`, state under
  `/var/lib/sandbox-science/middleware`, with its TLS certificate/key at
  `/private/middleware.crt` and `/private/middleware.key`. Configure the actual
  gateway verifier/audience and targets; listener TLS alone does not authenticate
  supervisor requests. OpenShell must trust this certificate and send tokens.
* Optional `cache.toml`: `listen = "0.0.0.0:8090"`, retained cache under
  `/var/lib/sandbox-science/package-cache`, bounded storage/temp settings, explicit
  package upstreams and `public_url` matching the reachable cache Service. The
  package cache uses its existing HTTP protocol; it is reachable only from approved
  sandbox peers under the rendered NetworkPolicy.

Certificates must cover their Service DNS names: `<name>-control.<namespace>.svc`,
`<name>-worker.<namespace>.svc`, `<name>-middleware.<namespace>.svc` and any exact
names selected by the operator. Runtime callback URL is
`https://<name>-control.<namespace>.svc:8443/control`, with its trusted CA supplied
through the runtime's private settings. Certificate and hostname verification must
remain enabled in clients. Kubelet HTTPS probes do not verify the server certificate;
validate client trust independently before claiming an encrypted working deployment.

If using [S3 artifacts](ARTIFACT-S3.md), both trusted control and workspace servers
open the artifact store: control needs its scoped retention/write access and the
workspace needs read-only GET/HEAD access to the same artifact prefix for cold
downloads. Give each its own standard-chain credential file/profile as above; no
credential is exposed to the browser. Worker operator checkpoint transport uses
a separate archive prefix and principal, independently of scientific artifacts.
No S3 key belongs in research sandboxes. Coordinate metadata, backing settings,
retained blobs, worker ownership/bindings and complete workspace checkpoints as a
recovery set. This bundle does not initialize project policy/bindings or restore it.

## Explicit network authority

Default-deny policies select only this bundle's trusted Pods. Additive allow rules
permit app-to-worker TCP 8100, approved `network.runtimes` to control TCP 8443,
the runtime manager's Pod (and nothing else) to control's manager port TCP 8444, and
approved `network.supervisors` to middleware TCP 50051. The runtime manager accepts
no connection; it reaches only DNS, control's TCP 8444 and the required
`network.gateway` peer and ports (the OpenShell gateway), which no other Pod gets
from the renderer. Cache TCP 8090 requires both
`secrets.cache` and nonempty `network.sandboxes`. These are approved peer selectors,
not a promise that arbitrary OpenShell Pods have the template's labels. Sandbox traffic leaves through each sandbox's OpenShell supervisor Pod, so
`network.runtimes` must select supervisor Pods; their labels are fixed by OpenShell, so
the rule admits every supervisor of the gateway, and the runtime token decides which
connection control accepts ([runtime bootstrap](specs/kubernetes-runtime-bootstrap.md)).
A selector peer in `network.runtimes` must therefore carry the pod labels
`openshell.ai/component = "supervisor"` and this gateway's `openshell.ai/gateway-id`.

Every peer uses either a canonical non-default CIDR or explicit namespace labels
with optional Pod labels. Empty selectors and `0.0.0.0/0` / `::/0` are refused. DNS
uses the explicit `network.dns` peer for TCP/UDP 53. Configure the actual CNI's DNS
labels/address; the example assumes conventional CoreDNS labels, which may differ.
Each `network.app_egress` or `network.pool_egress` entry names a peer and up to 16
TCP ports. Supply gateway, S3 and package upstream destinations explicitly:

```toml
[[network.pool_egress]]
ports = [443]
peer.cidr = "192.0.2.20/32" # documentation-only; replace with the approved private site value
```

No public/default egress, host/node authority or DNS-name-based network permission
is inferred. Dynamic external addresses need an operator-managed IP set or CNI
policy outside this renderer. CNI enforcement, Service DNAT ordering, node-local DNS,
host-network supervisors and OpenShell's independently scheduled supervisors need
live qualification. NetworkPolicy cannot authenticate a permitted IP/label, and
does not replace middleware token verification or OpenShell sandbox policy.

## Render, inspect, then qualify separately

```sh
sandbox-science-kubernetes check --config /path/to/private-kubernetes.toml
sandbox-science-kubernetes render --config /path/to/private-kubernetes.toml --output /path/to/new-bundle.json
```

Output is deterministic, atomically created mode 600 and never overwrites an existing
file. Parse/type/cross-setting errors do not print configuration values. Offline
validation checks only the renderer's configuration contract. Independently inspect
the JSON, service DNS/certificates, copied settings, node/resource/storage capacity
and NetworkPolicies before an operator chooses a cluster context and applies it.
`kubectl diff`/server dry-run can provide cluster admission evidence, but neither is
executed by this tool. The operator must also prepare the exact project policies
and recorded ownership.

Control readiness `/ready` is independent of runtime connectivity so that its
Service can accept the first authenticated runtime connection. `/live` is separate
process liveness. Worker/middleware/cache readiness uses a TCP probe without a bearer
token in the manifest; it proves only a listener. Perform passive authenticated
worker health/protocol checks with its scoped credential and CA, and inspect runtime
connection/drain state separately. Never treat a TCP probe, Pod restart or failed
runtime handshake as authority to create replacement work or replay an operation.

Workspace and preview listen only at `127.0.0.1:8200` and `127.0.0.1:8201` inside the
application Pod. No UI Service, ingress or forwarding-header trust is generated.
After deployment, select the exact application Pod and forward both ports directly:

```sh
kubectl -n research port-forward pod/EXACT_APPLICATION_POD 8200:8200 8201:8201
```

Open the workspace's existing login URL (its token is visible only through the
trusted operator's logs/workflow); retain the separate preview origin. Do not expose
this loopback login as a public ingress or add `--allow-remote` to work around it.
Public origins, secure cookies and authenticated ingress are a separate increment.
Stop/replace Pods only through a recovery-aware operator procedure, preserving PVCs,
original identities and unfinished effects. This offline increment does not qualify
cluster isolation, S3 durability, crash recovery, PVC deletion or either architecture.

## Bootstrap the agent runtime

The runtime manager starts the agent runtime only on an operator's command. After
the bundle is applied, control is ready and the manager is running:

```sh
kubectl -n research exec deploy/<name>-runtime-manager -- \
  sandbox-science-runtime-manager request bootstrap
kubectl -n research exec deploy/<name>-runtime-manager -- \
  sandbox-science-runtime-manager request status
```

Bootstrap requires that the manager's PVC holds no `sandbox.json`. It compiles the
runtime's policy, records a fresh `ss-orch-<10hex>` name, creates the sandbox from the
pinned `orchestrator` image under the configured Kata profile, records its ID before
waiting for Ready, asks control for a fresh generation and token, uploads them with
the model key, control CA and `runtime.json`, and runs the runtime in an exec session.
An empty PVC, failed handshake or missing record never creates a runtime. After that
the manager reconnects to the same recorded sandbox at its own start and after a
control restart; `request replace` replaces it explicitly, and `request stop --delete`
is the teardown. Deleting the manager Pod stops the runtime without deleting its
sandbox. The `orchestrator` image's OCI digest is distinct from its source revision,
which must match `control.json`'s `image_revision`.

Do not run `sandbox-science up` against this bundle as a bootstrap workaround: it
creates a local control instance, rotates generation/token and creates a new
runtime, breaking the cluster's intended original identity. Likewise, a failed
handshake or empty PVC never authorizes inventing a replacement runtime or clearing
ownership/recovery state. The manager and the bundle are not yet qualified on a live
Kata cluster ([KRB-27](specs/kubernetes-runtime-bootstrap.md#delivery-and-tests)).
