# Offline Kubernetes deployment bundle

The `sandbox-science-kubernetes check|render` command prepares a deterministic JSON
Kubernetes List. It reads only its explicit, bounded site-private TOML file. It does
not read kubeconfig or referenced Secrets, call `kubectl`, apply resources, build or
publish images, or create an OpenShell runtime. No actual cluster or native ARM64
deployment is qualified by rendering or schema validation.
Its `site` subcommand generates the Secrets' contents the bundle names, from a second
private file ([generate the Secrets](#generate-the-secrets)).

The trusted application image runs control and workspace in one Pod over one block
PVC; each container mounts only its own directories of it (`control/`, which the
workspace also reads as the harness's state, and its managed home/config
directories). A second Pod, the [worker-side unit](UNIT.md), runs worker and policy middleware over their shared block
PVC; package cache is optional there. Each of its containers runs `sandbox-science-unit serve --config /etc/sandbox-science/unit.toml --only <service>`, on one unit config the renderer makes as a ConfigMap `<name>-pool-unit` (no credential in it): a section for each service naming the settings file that service's Secret projects at `/private`, and the container's variables as the service's `pass_env`. The local launcher's unit uses the same file shape. The containers stay apart so that each keeps its own credentials, limits and probes; the Pod is ready when all are. A third Pod, the
[runtime manager](specs/kubernetes-runtime-bootstrap.md), runs the same
`trusted-app` image as `sandbox-science-runtime-manager serve` over a small PVC of its
own (the image carries the OpenShell CLI and the OpenSSH client that its
`openshell sandbox upload` needs). It alone holds an OpenShell gateway credential outside the worker, and it alone
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
`storage_class`. `image_pull_secret` (default `ghcr-pull`) names the registry Secret in
that namespace that every rendered Pod lists in `imagePullSecrets` (the app, the pool, the
runtime manager, the maintenance CronJob's, the release Job's and the checkpoint Jobs' Pods,
init containers included), since the images are private; checkpoint Jobs run in the sandbox
namespace, so the Secret must exist there too ([preparation, step 1](KUBERNETES-PREP.md#1-namespaces-and-the-pull-secret)). Use block-backed SQLite-compatible storage; RWX/NFS does not make
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
The renderer generates no Secret contents, tokens, settings or model credentials;
`sandbox-science-kubernetes site` generates them as files, with these bindings
([generate the Secrets](#generate-the-secrets)), and the operator creates the Secrets
from those files.
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
| `secrets.control` | `control.json`, `runtime-manager-token.sha256`, `worker-token`, `control.crt`, `control.key`, `worker-ca.crt`; with `[mcp_direct]` also `mcp-check-token.sha256` |
| `secrets.workspace` | `worker-token`, `worker-ca.crt`; with `[mcp_direct]` also `mcp-check-token`, which no other Secret may project |
| `secrets.worker` | `worker.toml`, `worker.crt`, `worker.key` |
| `secrets.middleware` | `middleware.toml`, `middleware.crt`, `middleware.key` |
| optional `secrets.cache` | `cache.toml` |
| `secrets.runtime_manager` | `runtime-manager.json`, `control-models.json`, `manager-control.token`, `model.key`, `control-ca.crt`, and the OpenShell CLI profile `active-gateway`, `gateway-metadata.json`, `gateway-ca.crt`, `gateway-tls.crt`, `gateway-tls.key`; with `[mcp_direct]` also `manager-worker.token`, `agent-mcp-project-policy.yaml`, `agent-mcp-ceiling.yaml` and `agent-mcp-targets.yaml` ([the direct MCP path](#the-direct-mcp-path-opt-in)) |

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

For example, grant the workspace a separate object-store principal and create its
private credential/config keys through the site's Secret workflow. The workspace reads
evicted content (cold downloads) and also **writes the originals of user uploads
itself**: once the backing is configured, an upload succeeds only after its verified
remote copy, so a read-only workspace principal makes every upload fail as unavailable
(the store answers 403). Give it write access to the artifact bucket if uploads are
used; a read-only principal suits a deployment that accepts no uploads:

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
profile separately under `control.environment` (control and the workspace both need
write on the artifacts bucket: control for publications, the workspace for upload
originals), and use another prefix-limited
principal for checkpoints ([workspace checkpoints](#workspace-checkpoints)). The
renderer validates file selection, not the permissions, certificate contents or SDK
profile correctness.

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
credential is exposed to the browser. Workspace checkpoints use buckets and keys of
their own, independently of scientific artifacts.
No S3 key belongs in research sandboxes. Coordinate metadata, backing settings,
retained blobs, worker ownership/bindings and complete workspace checkpoints as a
recovery set. This bundle does not initialize project policy/bindings or restore it.

## Generate the Secrets

```sh
sandbox-science-kubernetes site --config /path/to/private-site.toml --output /path/to/new-directory
```

`site` reads a second private file, [the site template](../examples/kubernetes/site.toml),
which names the bundle TOML (the renderer's own loader and checks read it, so the bundle's
name, namespace, Secrets and `[models]` catalog are given once) and adds what only the
Secrets hold. Like the bundle TOML it holds no secret value, only paths, relative to the
file:

| Key | What |
| --- | --- |
| `bundle`, `worker_policy`, `targets`, `ceiling` | The bundle TOML; the project policy granted to every project; the OpenShell targets file (`sandbox.middleware` must be `openshell.registration`, and with the cache its `package_cache` must be `<name>-cache.<namespace>.svc.<cluster_domain>:8090`); the pool ceiling. The policy library parses all three |
| `pool_id` | The worker's pool, the bundle's name by default |
| `cluster_domain` | The cluster's DNS domain, `cluster.local` by default (CoreDNS's `kubernetes <domain>` line: `kubectl -n kube-system get configmap coredns -o yaml`). OpenShell's sandbox DNS ignores the Pod's search domains, so a sandbox resolves only fully qualified names: every name a sandbox reaches is `<service>.<namespace>.svc.<cluster_domain>`: the package cache in `worker.toml`'s `package_cache`, the cache's `public_url` and the targets file, and, since the agent runtime runs in a sandbox, `runtime-manager.json`'s `control_url` (and `manager_url`, which must name the same host) and the runtime policy's control, worker and model hosts. Other trusted Pods keep the `.svc` names their search domains complete |
| `[[projects]]`, `[skills]` | As in the launcher's config (`id`, at most 63 characters, [see projects](PROJECTS-AND-SESSIONS.md#as-built-the-harnesss-project-records); `profile`, `sessions`, `skills`; `embedding_model`, `metadata_model`, `packs`); each profile must be one of `[profiles]` |
| `[model]` | The launcher's `[model]` without `name`, `delegated` and `reviewer`, which come from the bundle's catalog with its APIs and its efforts defaulted as the workspace's options default them (`max`, reviewer `medium`): `url`, `api_key_file` (the runtime manager's key), `watcher`, and `api`/`reasoning_effort` for aliases and roles outside the catalog. `url` is https, or http to an in-cluster Service (`*.svc`, `*.svc.<cluster_domain>`), whose key then crosses the Pod network unencrypted. The agent runtime reaches it from its sandbox, so an in-cluster name must be fully qualified (`http://litellm.litellm.svc.<cluster_domain>:4000/v1`); a bare `*.svc` name is refused |
| `[runtime]` | The `orchestrator` image by digest, its full source `revision`, `recover_infrastructure`, and its Kata placement |
| `[worker]` | `label_environments` (default `false`): `worker.toml`'s `openshell.environments.label_environments`, which gives each sandbox the worker creates the label `sandbox-science.io/environment=<environment ID>` and lets reconciliation of an unknown creation look for it. Confirmed on the test instance's OpenShell 0.1.5 gateway on 2026-10-26: the label appears on the OpenShell sandbox, not on the Kubernetes Sandbox object or its Pod, so select by it through `openshell sandbox list`, not `kubectl` |
| `[profiles.<name>]` | Each research base profile's image by digest and Kata placement |
| `[openshell]` | The gateway `endpoint` (`https://host:port`; only the trusted manager and worker use it, so `https://openshell.openshell.svc:8080` works on any cluster domain, and the chart's server certificate covers `openshell.openshell.svc`), `gateway_id` (`openshell`), the middleware's `registration` (`sandbox-science-policy`), optional `audience` and `admin_hosts`, `jwt_public_key` (the gateway's extension-token public key, from OpenShell's own Secrets), and the `manager` and `worker` client identities, each `ca`, `certificate` and `key` PEM paths issued by the gateway's client CA (prep step 5.1) |
| `[cache]` | Exactly when the bundle has `secrets.cache`: the package cache's settings besides its listener, URL and directories (`cache_dir`, and `worker_state_dir`, the worker's state on the same PVC, whose environments' lockfiles pin files against eviction). It must enable at least one ecosystem (`pypi`, `conda`, `cargo`, `go`, `npm`, `opam`, `hackage`; `{}` takes that ecosystem's defaults): a cache with none answers 404 to every request, so `site` refuses it |
| `[extra_files.<service>]` | A file for each other key the bundle's `extra_files` names, such as `s3-credentials` |

A placement is `runtime_class_name` (`kata` or `kata-*`, default `kata`), the
`node_selector` labels the site's Kata nodes carry, and `requests` and `limits`
(`cpu_millicores`, `memory_mib`, `ephemeral_storage_mib`); `kubernetes.io/arch = amd64` is
always added, since arm64 nodes cannot run Kata.

It writes into a new directory, or an empty one of the user's (it refuses a non-empty
one, as the renderer never overwrites its output), every file mode 600 and directory 700:

- `secrets/<service>/`: for each Secret, exactly the required keys and the bundle's
  `extra_files`. The worker's `extra_files` must name `pool-token`, `ceiling.yaml` and
  `targets.yaml`, the middleware's `ceiling.yaml` and `gateway-public.pem`; only the
  worker may name `pool-token`. `ceiling.yaml`, `targets.yaml` and `gateway-public.pem`
  are the site's files wherever a Secret names them; any other key comes from
  `[extra_files]`.
- `pool/worker-config/`: the worker's OpenShell CLI tree for the pool PVC
  (`openshell/active_gateway`, `openshell/gateways/<gateway>/metadata.json` and its
  `mtls/`), as `openshell gateway add` writes it, with `runtime_manager.openshell_gateway`
  as the gateway's name. The manager's profile keys are the same tree from its identity.
- `gateway/`: `middleware-ca.crt`, the CA the gateway must trust for the middleware, and
  `openshell-values-middleware.yaml`, OpenShell chart values that mount it from a
  ConfigMap `<name>-middleware-ca` at `/etc/openshell-middleware/<name>/ca.crt` and
  register the middleware under `gatewayConfig."openshell.supervisor"` (the chart takes
  literal dotted keys there; a nested `openshell:` map renders invalid TOML). The gateway
  reads `tls_ca_cert_path` itself and ships the PEM to the supervisors inside their
  configuration, so only the gateway's Pod needs the CA. The README gives the
  `kubectl create configmap` and `helm upgrade` commands; the gateway restarts and
  connects to the middleware at startup, exiting if it cannot, so apply the bundle first.
- `bootstrap/policy.yaml`, `secrets.sh` (one `kubectl create secret generic` per Secret,
  `--from-file` only, no value inline) and a `README.md` with the remaining steps and
  every `TODO(operator)`.

What it generates: a private CA (ECDSA P-256) and server certificates for
`<name>-control`, `<name>-worker` and `<name>-middleware` at `.<namespace>.svc` and
`.<namespace>.svc.<cluster_domain>`; a 32-byte random token for each link, without a
newline (control's `worker-token`, the workspace's, the worker's `pool-token` and the
manager's `manager-control.token`, whose lowercase SHA-256 hex is control's
`runtime-manager-token.sha256`); and the settings with the bindings above, through the
launcher's own builders (`control.json`'s model part and run settings, the worker's
client entries, the runtime's OpenShell target) and models (`RuntimeManagerSettings`).
`control-models.json` is `control.json`'s model part. The worker declares
`isolation_tier = 3`, `node_kernel_releases = ["*-talos"]` and a Kubernetes profile for
each base profile; `control.json` names the `trusted-app` image's Node and math checker
(`/usr/local/bin/node`, `/usr/share/sandbox-science/math`). The
middleware verifies the gateway's tokens with the given public key. The CA's key is
never written: renewing a certificate or rotating a token means generating a new
directory, replacing every Secret and recreating the Pods. The tests load each settings
file through the model its service starts with.

What it cannot generate, each a `TODO(operator)` in the README: applying the gateway's
values (merged by hand if the site's own OpenShell values already set
`server.extraVolumes`, `server.extraVolumeMounts` or `"openshell.supervisor"`, since Helm
does not merge lists), and the gateway's signing of extension tokens with the key whose
public half is `jwt_public_key`; the client identities, which the gateway's CA issues;
and the project initialization (`policy add`, `project create`, `session open`), whose
commands the README lists. `runtime-manager.json`'s `policy.worker_api` names the worker
Service only because `OrchestratorTarget` requires one; the runtime never calls it and
the pool's NetworkPolicy refuses it. Skill packs are not imported. With the bundle's
`[mcp_direct]` it also generates the [direct MCP path](#the-direct-mcp-path-opt-in)'s files;
the provider's Secret is the operator's.

## Explicit network authority

Default-deny policies select only this bundle's trusted Pods. Additive allow rules
permit app-to-worker TCP 8100, approved `network.runtimes` to control TCP 8443,
the runtime manager's Pod (and nothing else) to control's manager port TCP 8444, and
approved `network.supervisors` and the `network.gateway` peer to middleware TCP 50051
(OpenShell 0.1.5's gateway connects to each registered middleware at startup and exits,
"middleware registration failed", if it cannot). The runtime manager accepts
no connection; it reaches only DNS, control's TCP 8444 and the required
`network.gateway` peer and ports (the OpenShell gateway). The pool Pod reaches the same
gateway peer and ports, since the worker creates every research sandbox through the
gateway with its own client identity; the app Pod does not. Cache TCP 8090 requires both
`secrets.cache` and nonempty `network.sandboxes`. With `[mcp_direct]`, the runtime manager
also reaches the worker's TCP 8100, and the pool admits it there, to bind the runtime's
sandbox. These are approved peer selectors,
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

## The direct MCP path (opt-in)

Without it the agent has no connectors. With it, the agent's connector calls take the
direct path (D98, [MCP connectors](MCP-CONNECTORS.md#the-direct-path)): control's run
service admits each call and the agent runtime's own client makes it from its sandbox,
through OpenShell's native `protocol: mcp` rules and the policy middleware. It is the only
MCP path since the MCP proxy retired (2026-10-26). Nothing of the proxy is rendered.

```toml
[mcp_direct]
project = "crypto"                        # a site project: the policy the middleware applies
provider = "crypto-research-mcp"          # the OpenShell provider (the default)
provider_secret = "research-mcp-provider" # the Secret the operator creates (below)
# index = ["crypto-research-mcp/e-print-mcp"]
# [mcp_direct.canaries]                   # on by default, as below
# enabled = true
# interval = 900                          # seconds, 60 to 86400
[mcp_direct.servers.crypto-research-mcp]
credentials = { "CF-Access-Client-Id" = "CF_ACCESS_CLIENT_ID", "CF-Access-Client-Secret" = "CF_ACCESS_CLIENT_SECRET" }
[mcp_direct.about]
"crypto-research-mcp" = "Cryptography research servers"
```

Each server must be one of the site policy's, whose URL it takes, and one the floor and the
pool ceiling admit. With it:

- **The renderer** gives the runtime manager's Pod an init container,
  `register-mcp-provider`, after its copy init: it runs
  `python3 -I -m sandbox_science.launcher.access_provider --from-environment` with the
  manager's OpenShell CLI profile, and the Secret `provider_secret`'s two keys,
  `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET`, as its environment (each by name, with
  `secretKeyRef`: never any other key of the Secret) and no other container's, and creates the
  provider in the gateway, or updates it, before the manager starts. The Secret holds those two
  keys only; the operator makes it from a file
  ([prep](KUBERNETES-PREP.md#the-direct-mcp-paths-provider)). The manager's NetworkPolicy
  adds the worker's TCP 8100, and the pool's admits the manager there.
- **The site generator** compiles the runtime sandbox's egress from the site's policy, pool
  ceiling and targets (`launcher.direct_mcp`, as `sandbox-science up` does) to refuse a
  bundle whose files do not compile: one inspected `protocol: mcp` endpoint per server,
  bound to the provider's credentials and admitting only the runtime's interpreter, and the
  policy middleware (the targets file's registration) for their hosts, with the project and
  the caller `agent-runtime`. It writes the three files into the manager's Secret
  (`agent-mcp-project-policy.yaml`, `agent-mcp-ceiling.yaml`, `agent-mcp-targets.yaml`),
  from which the manager compiles the egress itself, and a token for the worker,
  `manager-worker.token`, generated apart from the manager's control token (which control sees
  on every manager request), whose client (`manager`) the worker knows as `binding_only` for
  that project alone: it may bind and unbind the runtime's sandbox and read the project's
  classification revisions in force (`GET /v1/runtime-classifications`), and reach no other
  route ([worker](WORKER.md)).
  `runtime-manager.json` gets the `worker` link (the worker Service, verified with the
  private CA in `control-ca.crt`) and `mcp` (the three files, the servers with their
  credential headers, and the provider);
  `control.json` gets `direct` (the run service's servers, `about` and `index`),
  `canaries` (the direct path's project and servers, every `[mcp_direct.canaries]` `interval`
  seconds, 900 by default, as the host launcher writes them; control refuses the
  workspace's listing report without it, so "Check availability" would return
  `unavailable`; `enabled = false` omits it) and
  `check_token_sha256_file`, the digest (`mcp-check-token.sha256`) of the workspace's token
  for the user's availability check (`mcp-check-token`, in the workspace's Secret alone); and
  `middleware.toml` gets `mcp_listings = true` and `[inspect]` at the middleware's defaults
  (on; 16 KiB of arguments, 64 KiB of each answer and 8 MiB a run recorded; the shipped
  patterns), which the operator may change ([MCP traffic
  inspection](POLICY-MIDDLEWARE.md#mcp-traffic-inspection)).
- **The workspace** gets the MCP services view as `sandbox-science up` wires it: the
  project and its servers with their `about` purposes (`--mcp-project`, `--mcp-service`), and
  the user's **Check availability**, which asks control's `POST /mcp-check` by control's
  Service name, `https://<name>-control.<namespace>.svc:8443`, with its own token
  (`--mcp-check-token-file`) and the site's private CA (`--mcp-check-ca-file`, its
  `worker-ca.crt`, which signs control's certificate too). The app Pod's NetworkPolicy
  admits the app Pod itself to TCP 8443, and lets it reach it, for that call alone.
- **The runtime manager** compiles the egress at each creation of the runtime's sandbox,
  bootstrap and replacement alike (`runtime_manager.fence`), with the project's
  classification in force: it reads each server's revision through the worker (the file as
  stored, which it checks against its digest) and compiles with it where one is bound, else
  with the checked-in file, as the host launcher does. It merges the egress into the
  sandbox's policy beside its platform services, attaches the provider to the sandbox
  (`--provider`), labels it with its project (`sandbox-science.io/project`, which the worker
  checks before binding), binds the sandbox to the project's revision through the worker
  once it is Ready, and gives the runtime its servers in `runtime.json`, with the tools that
  sandbox's fence admits (kept as `runtime-mcp.json` in its state, for every attach to the
  sandbox). It never reads the provider's values.

The egress is compiled from the site's policy file: after a new policy revision, generate the
Secrets anew. A classification revision accepted while the runtime runs reaches its fence at
the next replacement (`sandbox-science-runtime-manager request replace`, or an automatic one);
the middleware decides each call by the revision in force meanwhile, so the stale fence
refuses a widening and never admits more than the middleware allows. A binding the worker
cannot read leaves the servers concerned on the checked-in file with a warning in the
manager's log, and the middleware refuses their calls; a worker that does not answer, a file
that does not check against its digest, or a revision that does not compile into a fence
leaves them on the checked-in file too, while the middleware decides by the classification
in force. The manager compiles before it deletes the old sandbox
([project revisions](MCP-CONNECTORS.md#project-revisions)).

## Job sandboxes and node drains

An optional `[sandboxes]` section names the namespace in which OpenShell creates sandboxes
(`namespace`, the chart's `server.sandboxNamespace`; the renderer cannot derive it from the
`network.*` peers, which select it by label). With it the bundle adds one object outside its
own namespace: the `PodDisruptionBudget` `<name>-job-sandboxes` there, `maxUnavailable: 0`,
selecting `sandbox-science.io/job-sandbox: "true"`, the label the worker puts on a job
sandbox's workload Pod. A `kubectl drain` then waits for a running job instead of evicting
it ([job sandboxes](specs/job-sandboxes.md), JS-20); a running Pod that is not Ready may
still be evicted (`unhealthyPodEvictionPolicy: AlwaysAllow`), so a wedged one cannot hold a node.
The budget protects nothing from crashes, power loss, `--disable-eviction` or a drain
that gives up. The gateway must also list the label key in `allowed_pod_labels`
([gateway settings](KUBERNETES.md#gateway-settings-for-profile-placement)), or no job
sandbox starts.

Without `[sandboxes]` nothing is rendered outside `namespace`, and no budget exists. With
it, applying the bundle needs permission to create a PodDisruptionBudget in the sandbox
namespace as well as in the bundle's. A budget is per namespace: when two instances share a
sandbox namespace, set `job_budget = false` in all but one, since two budgets selecting the
same Pod make every eviction of it fail. A budget with no job profile configured selects no
Pod and costs nothing. Check it with `kubectl -n <sandbox-namespace> get pdb` and
`kubectl drain <node> --dry-run=server`.

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

By default (no `[ingress]` section) workspace and preview listen only at
`127.0.0.1:8200` and `127.0.0.1:8201` inside the application Pod. No UI Service,
ingress or forwarding-header trust is generated. After deployment, select the exact
application Pod and forward both ports directly:

```sh
kubectl -n research port-forward pod/EXACT_APPLICATION_POD 8200:8200 8201:8201
```

Open the workspace's existing login URL (its token is visible only through the
trusted operator's logs/workflow); retain the separate preview origin. Do not expose
this loopback login by other means or add `--allow-remote` to work around it: the
only supported route off loopback is the opt-in
[ingress](#workspace-through-the-clusters-ingress-opt-in).
Stop/replace Pods only through a recovery-aware operator procedure, preserving PVCs,
original identities and unfinished effects. This offline increment does not qualify
cluster isolation, S3 durability, crash recovery, PVC deletion or either architecture.

## Workspace through the cluster's ingress (opt-in)

An `[ingress]` section serves the workspace and its preview origin through the cluster's
Traefik, over TLS, behind Authentik, instead of `kubectl port-forward` (D92). Without it
nothing in this section is rendered and the Pod keeps its loopback listeners. The
renderer makes objects only; the operator creates the Authentik provider, the forward-auth
`Middleware`, DNS and the issuer ([preparation, step 12](KUBERNETES-PREP.md#12-workspace-through-the-load-balancer-optional)).

```toml
[workspace]
allow_remote = true               # required with [ingress], refused without it

[ingress]
workspace_host = "workspace.example.org"   # documentation-only names
preview_host = "preview.example.org"       # a separate origin: a name of its own
issuer = "YOUR_CLUSTER_ISSUER"             # cert-manager issuer; issuer_kind defaults to ClusterIssuer
authentik_middleware = "authentik-forward-auth"  # a Traefik Middleware in this namespace
# entry_point = "websecure"       # the HTTPS entry point (default); "web" and "http" are refused
# ingress_class = "traefik-public" # sets kubernetes.io/ingress.class on the routes, to pick one controller

[[ingress.traefik]]               # the controller's Pods, the only peers let in (1 to 4)
namespace_labels = { "kubernetes.io/metadata.name" = "traefik-public" }
pod_labels = { "app.kubernetes.io/name" = "traefik" }
```

**Validation.** Host names are lowercase DNS names of at least two labels (no wildcard, IP
address, trailing dot or quote, since the name goes into a route rule); the two differ; the
issuer and middleware are Kubernetes names; the entry point may not be `web` or `http`
(TLS required: every route has a `tls` block and its certificate); each controller peer
selects Pods by namespace labels and Pod labels, never a bare namespace or a CIDR;
`workspace.allow_remote` is refused without `[ingress]`, and `[ingress]` is refused without
it (the explicit acknowledgement that the plain-HTTP listener leaves loopback). The
section has no field that turns authentication off.

**What changes.** The workspace container listens on `0.0.0.0:8200` and `0.0.0.0:8201`
and gets `--allow-remote --public-origin https://<workspace_host> --preview-public-origin
https://<preview_host>`; it declares those two ports. For
each host name the renderer adds:

* a `ClusterIP` Service, `<name>-workspace` (8200) and `<name>-preview` (8201), selecting
  the application Pod; control's ports are in no Service the ingress reaches;
* a cert-manager `Certificate` of the same name for that one host, into the Secret
  `<name>-workspace-tls` or `<name>-preview-tls`;
* a Traefik `IngressRoute` (`traefik.io/v1alpha1`) of the same name: the HTTPS entry point,
  a `Host(...)` rule, the Authentik `Middleware` by name and that Secret as its TLS;
* one additive NetworkPolicy, `<name>-ingress`, on the application Pod: from the
  `[[ingress.traefik]]` peers to TCP 8200 and 8201, and nothing else. The default-deny and
  the application policy stay as they are, so no other Pod reaches those ports and nothing
  else is opened.

**Why `IngressRoute`, not `Ingress`.** The Authentik check is a Traefik `forwardAuth`
`Middleware`. An `IngressRoute` names it (and the TLS Secret) as fields and sets the entry
point directly; an `Ingress` can only name it in a `router.middlewares` annotation as
`<namespace>-<name>@kubernetescrd`, an untyped string. cert-manager's annotation shortcut
works only on `Ingress` and `Gateway` objects, so the bundle renders the `Certificate`
explicitly, which also names the Secret the route reads. The cost is a dependency on
Traefik's CRDs (a cluster that moves to another controller needs another renderer).

**What the workspace does behind the route** ([workspace: behind a reverse
proxy](WORKSPACE.md#behind-a-reverse-proxy)). Told its two public origins, the workspace:

* answers only to a `Host` naming `<workspace_host>` (the viewer only `<preview_host>`), so
  a `kubectl port-forward` to the Pod's listener is refused; Traefik passes `Host` through;
* takes a change only with `Origin: https://<workspace_host>`. Traefik must not rewrite
  `Origin`;
* sets the session cookie Secure, HttpOnly, SameSite=Strict and host-only, named
  `__Host-sandbox_science_workspace-8200`, so the preview host cannot set or shadow it.
  Redirect the `web` entry point to HTTPS at Traefik and consider HSTS on the routes'
  hosts, which a `Headers` middleware can add;
* announces the preview as `https://<preview_host>` in its CSP and links and the viewer's
  frame ancestors, so the PDF viewer and figure frames work through the ingress.

The renderer passes no `--trust-proxy-from`: Traefik's Pods have no fixed addresses, and
with `Host` passed through no forwarded header is needed. Authentik is checked before
the workspace's own login token, which the workspace still needs unless
[login through Authentik](#login-through-authentik-opt-in) is on
(`kubectl logs deploy/<name>-app -c workspace` prints the login URL on the public origin; with login through Authentik it is not in the log, and `kubectl -n <namespace> exec deploy/<name>-app -c workspace -- sandbox-science-workspace login-url --state-dir /var/lib/sandbox-science/control` prints it);
Authentik's plain headers open no workspace session. Nothing here is qualified on a cluster.

### Login through Authentik (opt-in)

Owner decision 2026-10-28 (D104). `[ingress.forward_auth]` lets a user Authentik admits
into the workspace without the login URL, by the token Authentik's proxy outpost signs
([workspace: login through Authentik](WORKSPACE.md#login-through-authentik)). Absent, or
with `enabled = false`, nothing of it is rendered. `algorithm` is required and pinned:

* **`HS256`** (Authentik's proxy providers): the token's MAC under the provider's client
  secret. Authentik 2026.8 gives a proxy provider no signing key it keeps:
  `ProxyProvider.set_oauth_defaults` (`authentik/providers/proxy/models.py`) sets
  `signing_key` to None whenever the server starts, so the outpost's token is always
  HS256, whatever the provider's settings say. The site generator puts the secret into
  the workspace's Secret, and the workspace reaches nothing.
* **`RS256`**: a signature by a provider's signing key, checked against its JWKS
  (`jwks_url`), fetched from the `authentik` peer: for a provider whose signing key stays.

```toml
[ingress.forward_auth]
# enabled = true
algorithm = "HS256"
issuer = "https://auth.example.org/application/o/cryptoatlas/"
audience = "PROVIDER_CLIENT_ID"    # the client ID: public, not the client secret
user_group = "atlas-users"
admin_group = "atlas-admins"       # optional: absent, every user may take the admin-only actions
# header = "X-authentik-jwt"
# RS256 instead:
# algorithm = "RS256"
# jwks_url = "http://authentik-server.authentik.svc/application/o/cryptoatlas/jwks/"
# authentik_ports = [9000]         # the server Pods' HTTP port, the Service's target
# authentik = { namespace_labels = { "kubernetes.io/metadata.name" = "authentik" }, pod_labels = { "app.kubernetes.io/name" = "authentik", "app.kubernetes.io/component" = "server" } }
```

With HS256, the site file names the secret's file
(`forward_auth_secret = "secure/authentik-forward-auth-client-secret"`, mode 600).

**Validation.** `algorithm` is `HS256` or `RS256`; HS256 takes no `jwks_url` or
`authentik`, and RS256 needs both. `jwks_url` is `https`, or `http` to an in-cluster
Service (`*.svc` or `*.svc.<domain>`), with no credentials or query; `issuer` is a URL; the
groups are names without control characters; `header` is letters, digits and hyphens;
`authentik` selects Pods by namespace and Pod labels (or a CIDR), never a bare namespace.
Unknown keys are refused, so no secret can be put in the bundle; no service's
`extra_files` may name `authentik-client-secret`. The site generator refuses an HS256
bundle without `forward_auth_secret`, a missing file, one open to other users or one
shorter than 32 bytes, and a `forward_auth_secret` without HS256.

**What changes.** The workspace gets `--forward-auth-algorithm`, with HS256
`--forward-auth-secret-file /private/authentik-client-secret` (the workspace's Secret's key
`authentik-client-secret`, which only its copy init projects, mode 600) or with RS256
`--forward-auth-jwks-url`, and `--forward-auth-issuer`, `--forward-auth-audience`,
`--forward-auth-user-group`, `--forward-auth-header` and, with an admin group,
`--forward-auth-admin-group`. With HS256 no NetworkPolicy changes. With RS256 the
`<name>-ingress` NetworkPolicy gains an egress rule, its only one: the application Pod to
the `authentik` peer's Pods on `authentik_ports`, for the JWKS. A NetworkPolicy selects
Pods, not containers, so that rule admits every container of the application Pod,
`control` (zone Z2) included, to Authentik's server on that port, with no credential of
Authentik's; narrowing it to the JWKS path would need a proxy in front of Authentik. The
JWKS crosses the Pod network in plain HTTP, as every request from Traefik to the workspace
does, so this adds no new exposure ([threat model](THREAT-MODEL.md)). The workspace no
longer prints its login URL, which logs in as the operator, an admin:
`kubectl -n <namespace> exec deploy/<name>-app -c workspace -- sandbox-science-workspace login-url --state-dir /var/lib/sandbox-science/control` prints it.

**Who can mint a login.** With HS256, whoever holds the client secret can make a token for
any user and group: Authentik's administrators, and whoever can read the workspace's
Secret, the site's `secure/` file or the workspace container's `/private`. That is the
same reach as the login link's, which logs in as the operator.

**The operator's side** (the site generator's README lists it too):

* with HS256, write the provider's client secret into the site's private file once,
  straight from `ak shell`, so that it is never printed or kept in shell history:

  ```sh
  umask 077 && mkdir -p secure
  printf '%s\n' 'from authentik.providers.proxy.models import ProxyProvider' \
    'print("CLIENT_SECRET=" + ProxyProvider.objects.get(name="PROVIDER_NAME").client_secret)' |
    kubectl -n authentik exec -i deploy/authentik-server -- ak shell 2>/dev/null |
    sed -n 's/^CLIENT_SECRET=//p' > secure/authentik-forward-auth-client-secret
  test -s secure/authentik-forward-auth-client-secret && wc -c < secure/authentik-forward-auth-client-secret
  ```

  `wc -c` shows its length only (129 for Authentik's 128-character secrets and the
  newline). Then generate the Secrets and apply them (`secrets.sh`). Rotating the secret
  in Authentik means doing this again; the workspace reads the file at start and when it
  changes, and refuses every token while it is unusable;
* with RS256, give the provider a **Signing Key** (*Advanced protocol settings*); on
  Authentik 2026.8 a proxy provider does not keep it (above);
* add `X-authentik-jwt` to the forward-auth middleware's `authResponseHeaders`;
* create the admin group, if one is configured, and add the admins to it directly: the
  token lists a user's direct groups only.

The issuer is `https://<Authentik host>/application/o/<slug>/`, as the provider's discovery
document `https://<Authentik host>/application/o/<slug>/.well-known/openid-configuration`
says (with `jwks_uri`, whose path an in-cluster RS256 `jwks_url` keeps; use the cluster's
DNS domain there if search domains do not resolve `*.svc` in the Pod).

**Both host names behind Authentik.** The preview is framed by the workspace page, so its
requests carry the Authentik session too. Put both names under one parent domain and use
one Authentik proxy provider in domain-level mode; names on unrelated domains make the
frame a third-party context and the browser may drop the cookie. Same-site names are still
separate origins, which is all D54 needs; the viewer's frames may share the workspace
page's thread (FG-17, [preview](WORKSPACE.md#the-pdf-viewers-origin)).

## Maintenance

Workspace checkpoints to object storage, the third item on D92's list, run as Jobs in the
sandbox namespace ([workspace checkpoints](#workspace-checkpoints)).

Two maintenance workloads from D92's list before a research stress test: scheduled
control-cache trimming, rendered into the bundle by an opt-in `[maintenance]` section, and
the offline worker replica release, a Job the operator creates by hand. Neither Pod mounts
a service-account token or has any cluster authority, and both run the app's or pool's
image with the same security context and JSON logs (`SS_LOG_FORMAT=json`) as the
Deployments. Neither is qualified on a cluster yet.

### Cache trimming (opt-in)

```toml
[maintenance]
# schedule = "@daily"          # five cron fields or a macro (@hourly, @daily, ...), controller time zone
# at_use_percent = 70          # pass only once the app volume is at least this full (1-100)
# max_references = 25          # the retention policy's bounds per pass (1-100 hashes)
# max_bytes = 1073741824       # (1 B to 64 GiB)
# min_age_seconds = 86400      # keep local copies younger than this
# deadline_seconds = 3600      # the Job's activeDeadlineSeconds (60 to 86400)
```

With the section, the bundle adds three objects in its namespace:

* the ConfigMap `<name>-maintenance-policy`, `artifact-retention.toml`: the
  [example policy](../examples/kubernetes/artifact-retention.toml) with the section's
  bounds, `archive_before_evict = false`;
* the CronJob `<name>-maintenance` (`batch/v1`), `concurrencyPolicy: Forbid`,
  `backoffLimit: 0`, the section's `activeDeadlineSeconds`, three finished Jobs of each
  outcome kept. Its Pod runs
  `sandbox-science-artifacts maintain --database /var/lib/sandbox-science/control/control.sqlite3 --policy /etc/sandbox-science/maintenance/artifact-retention.toml --apply --cursor-file /var/lib/sandbox-science/control/maintenance-apply-<policy digest>.json --at-use-percent <N>`
  on the app image, with the app PVC's `control` directory mounted. The PVC is
  ReadWriteOnce, so the Pod has a required Pod affinity to the app Pod's node, and waits
  (until the deadline) while the app Pod is down. The cursor's name carries the policy's
  digest: the cursor is bound to one policy, so changing the section starts a new progress
  file instead of failing every pass;
* the NetworkPolicy `<name>-maintenance`: nothing may connect to the Pod; it reaches DNS
  and the `network.app_egress` flows, where the object store is. Without the store the
  pass reports `unavailable` and keeps every local copy.

The Pod gets no control credential. When control's `environment` names object-store SDK
files (`AWS_SHARED_CREDENTIALS_FILE`, `AWS_CONFIG_FILE`, `AWS_CA_BUNDLE`), a `copy-storage`
init container copies exactly those keys of control's Secret into the Pod's memory volume,
and the container gets those variables and `AWS_PROFILE`/`AWS_REGION`; `control.key`,
the tokens and `control.json` stay in the app Pod.

`--at-use-percent` makes the schedule implement "trim at about 70% use" without a
controller: each pass measures the control volume (`statvfs` of the database's directory,
as `df` counts it) and exits 0, reading and writing nothing, below the threshold
([bounded cache maintenance](ARTIFACT-S3.md#bounded-cache-maintenance)). A pass above it
trims at most `max_bytes` of verified, remotely retained content, so a volume filling
faster than that needs a tighter schedule or a larger bound. Check it with
`kubectl -n <namespace> get cronjob,jobs -l app.kubernetes.io/component=maintenance` and
the Job's log, one JSON report per pass; `kubectl create job --from=cronjob/<name>-maintenance <name>-maintenance-now`
runs one pass at once.

To see a pass evict on a volume that is still nearly empty, the lowest threshold is 1%:
render a one-off with `at_use_percent = 1` (and a short `min_age_seconds`), raise the
volume's use past 1% with a temporary file on the app PVC if needed, run one Job from the
CronJob, then remove the file and render the section back.

### Offline worker replica release

The worker's offline replica release
([offline capture replica retention](WORKER.md#offline-capture-replica-retention)) unlinks
CAS replicas that control durably retained and acknowledged through its outbox. It needs
the worker stopped, so it is never part of `render` or any schedule:

```sh
sandbox-science-kubernetes release-job --config private.toml --output release-preview.json --preview
sandbox-science-kubernetes release-job --config private.toml --output release.json
sandbox-science-kubernetes release-job --config private.toml --output release-2.json --after NEXT_AFTER
```

Each file is one Job (`batch/v1`) with `generateName: <name>-worker-release-`, so
`kubectl create -f` makes a new one each time and `kubectl apply` refuses it. It runs
`sandbox-science-worker artifact-retention --settings /private/worker.toml --limit 100`
(`--apply` unless `--preview`; `--after` for the next page) on the worker image, with the
pool PVC, the worker's Secret copied as in the pool Pod, `backoffLimit: 0`, a one-hour
deadline and a week before the finished Job is deleted. Its Pod is labelled
`app.kubernetes.io/component: worker-release`: only the default-deny NetworkPolicy selects
it, so it has no network, and no Service selects it.

The sequence:

1. Optionally drain pending acknowledgements first, a page per project, with
   `sandbox-science-artifacts acknowledge` in the app Pod's control container
   ([acknowledging durable worker copies](ARTIFACTS.md#acknowledging-durable-worker-copies)).
   A hash without an exact acknowledgement for every reference is kept, so skipping this
   releases less, never more.
2. Scale the pool to zero and wait for its Pod to go:
   `kubectl -n <namespace> scale deploy/<name>-pool --replicas=0`, then
   `kubectl -n <namespace> wait --for=delete pod -l app.kubernetes.io/component=pool --timeout=300s`.
3. Create the preview Job, `kubectl -n <namespace> create -f release-preview.json`, wait
   for it (`kubectl -n <namespace> wait --for=condition=complete job/<generated name> --timeout=3600s`)
   and read its report: `kubectl -n <namespace> logs job/<generated name>`, one line per
   hash with its status and reason, then `next_after=<hash>` or `next_after=none`.
4. Create the apply Job the same way, and read its report. While `next_after` names a
   hash, render and create the next page with `--after`.
5. Scale the pool back: `kubectl -n <namespace> scale deploy/<name>-pool --replicas=1`.

**The guard** is the worker's own: the command takes the exclusive `StateLock`
(`worker.lock` in the pool's state directory) and refuses, exiting 1 with "another worker
uses the state directory", while a worker holds it. That lock holds between Pods on one
node, which share the volume's file system. On another node the pool PVC, ReadWriteOnce,
cannot be attached while the pool Pod has it, so the Job's Pod waits (and fails at its
deadline) instead of running beside a worker; a pool Pod started while the Job runs is
refused the same way. The Job has no preflight that lists Pods, which would need a
service account with authority to read them. Online release, beside a serving worker,
is not built: it still needs the reader and writer fences
([Kubernetes work](KUBERNETES.md)).

## Workspace checkpoints

A checkpoint is a verified full copy of one research workspace in the object store (D92):
the worker's checkpoint format, streamed in 16 MiB content-addressed chunks with a manifest
whose SHA-256 is the checkpoint's identity ([checkpoint Jobs](WORKER.md#checkpoint-jobs)).
The workspace volume lives in the sandbox namespace and the worker's state in the bundle's,
and a Pod mounts claims of its own namespace only, so a short-lived **checkpoint Job** reads
the volume beside it, and the worker checks the result in the store:

1. **Plan** (the worker, online): it holds the environment stopped, suspending it through its
   own API if it runs (refused while it is in use: a job, a publication, busy kernels), and
   prints the plan: the retained binding to write into the archive, a checkpoint ID and the
   hold's expiry (6 hours by default). While held, the worker refuses to start the sandbox:
   each start checks for a hold and records itself in one transaction, a plan refuses while
   a start is under way, and records it cannot read refuse the start.
2. **The Job** (`checkpoint-job`, created by the operator): `sandbox-science-worker
   checkpoint capture-upload` on the trusted worker image, the workspace claim mounted
   read-only, run as the sandbox user (10001) so that a file the sandbox made unreadable
   fails it by name, pinned to the volume's node. It streams to the **upload bucket** with
   the upload key, re-sending only chunks that do not already read back exactly, and prints
   `checkpoint_manifest=<sha256>`. Sockets, FIFOs and devices are left out and listed in the
   receipt. No local archive: its scratch holds one chunk and the path index.
3. **Verify** (the worker): it reads the manifest and every chunk back through its decoder,
   against its own binding, the planned capture mode and the hold's time window (ending at
   an early release), records the checkpoint as `verified` with its chunk list and lifts the
   hold. It holds only a read key.
4. **Promote** (the operator's machine, keeper key): the manifest's chunks, then the manifest,
   are copied into the **kept bucket** inside the store (`CopyObject`). Chunks are shared
   between checkpoints of a workspace, and Garage ignores `If-None-Match`, so a kept object
   that already reads back with its hash is never overwritten; any other is read and hashed
   in the upload bucket, copied only as that version (`CopySourceIfMatch` on its ETag) and
   hashed again in the kept bucket. A chunk swapped in the upload bucket after verification
   therefore fails the promotion, and cannot reach an older kept checkpoint.
5. **Verify again** (the worker): the kept copy, byte for byte; the checkpoint is now `kept`,
   the only state that counts. The latest two kept checkpoints of a workspace stay; older
   ones are listed by [cleanup](#cleanup) and removed only when named there, and the latest
   two go only when the operator retires a deleted environment's checkpoints there.

```toml
[checkpoint]                           # needs [sandboxes]
upload_secret = "research-checkpoint-upload"   # in the sandbox namespace: credentials, config
upload_bucket = "research-checkpoint-upload"
kept_bucket = "research-checkpoints"
endpoint = "http://garage.garage.svc.cluster.local:3900"
# region = "garage", prefix = "sandbox-science", addressing_style = "path"
# upload_profile = "checkpoint-upload", verify_profile = "checkpoint-verify"
# restore_secret = "research-checkpoint-restore"   # restore Jobs only; absent, no restore
# restore_profile = "checkpoint-restore"
# restore_run_as_user = 1000, restore_run_as_group = 1000   # the images' sandbox user (D77)
# keep = 2, deadline_seconds = 7200, hold_seconds = 21600 (longer than the deadline)
# job_ttl_seconds = 600 (how long Kubernetes keeps a finished Job; 0 to 604800)
# run_as_user = 10001, run_as_group = 10001
# [checkpoint.job]: cpu/memory/ephemeral requests and limits, scratch_mib (default 1024)

[checkpoint.object_store]              # the store's Pods and port
ports = [3900]
peer.namespace_labels = { "kubernetes.io/metadata.name" = "garage" }
peer.pod_labels = { "app.kubernetes.io/name" = "garage" }
```

With the section the bundle renders the NetworkPolicy `<name>-checkpoint` in the sandbox
namespace (nothing connects to a checkpoint Pod; it reaches DNS and the store only, never
the gateway, a sandbox or the internet) and adds the store to the pool's egress. The worker's
Secret then also projects `checkpoint-upload.toml` and `checkpoint-kept.toml` (destinations,
no credential) and `checkpoint-s3-credentials`, `checkpoint-s3-config` (its read-only key);
`site` generates them from the site file's `[checkpoint]` paths, writes the upload key's
Secret files and its `kubectl create secret` line for the sandbox namespace (and the restore
key's, with `restore_secret`), and writes
`operator/checkpoint-upload.toml` and `operator/checkpoint-kept.toml`, the buckets as the
operator's machine reaches them through `kubectl port-forward`. The keys and buckets are in
[preparation, step 6](KUBERNETES-PREP.md#6-garage-artifacts).

The Job, as rendered: `generateName: <name>-checkpoint-` (so `kubectl apply` refuses it),
`backoffLimit: 0`, the section's deadline, removed by Kubernetes `job_ttl_seconds` (600 s)
after it ends (`ttlSecondsAfterFinished`: its finished Pod still names the workspace claim,
which keeps a deleted claim `Terminating`); no service-account
token, no service links, no `fsGroup` (the kubelet would otherwise change the workspace's
group), a read-only root, no capabilities, `RuntimeDefault` seccomp; the claim and the upload
Secret (its `credentials` and `config` keys only) read-only and a bounded `/tmp`. The plan
and the upload bucket's settings are its environment (`SS_CHECKPOINT_PLAN`,
`SS_CHECKPOINT_STORAGE`); it never sees the kept bucket. NetworkPolicies add up, so a sandbox
Pod with the Job's labels would get the same egress; the worker never sets
`app.kubernetes.io` labels on sandboxes.

One command does it all. The promotion runs on the operator's machine with the keeper key,
so that machine's checkout needs the S3 SDK: `uv sync --extra s3` (or `--all-extras`); with
keeper files given, the command asks the local `sandbox-science-worker` that will run the
promotion (`checkpoint sdk-check`) and refuses before anything runs when boto3 is missing.

```sh
sandbox-science-kubernetes checkpoint --config private.toml --environment ENV          # prints the steps
kubectl -n garage port-forward svc/garage 3900:3900 &
AWS_PROFILE=checkpoint-keeper sandbox-science-kubernetes checkpoint --config private.toml \
  --environment ENV --apply \
  --keeper-upload operator/checkpoint-upload.toml --keeper-kept operator/checkpoint-kept.toml
```

With `--apply` it runs, with the operator's kubeconfig: the plan (`kubectl exec` into the
pool's worker container), the claim and its node (`volume.kubernetes.io/selected-node`),
refusing while any Pod mounts the claim; `kubectl create` of the Job; polling until it
succeeds or fails; its log for the manifest; the worker's verification; then it prints the
Job's log (`job <name>: …` lines, the manifest and any skipped files) and deletes the Job,
whose finished Pod would otherwise hold the claim (a failed deletion is reported and the
run goes on; the Job's TTL removes it later); the promotion (locally, `sandbox-science-worker
checkpoint promote`); the worker's second verification. The claim is found by its owning
`Sandbox`, whose name OpenShell prefixes with its workspace (see [cleanup](#cleanup)).
A failure before the first verification succeeds deletes the Job and lifts the hold; if
that cleanup fails too, the error says so after the run's own error. Without the keeper files it
stops after the first verification and prints the two remaining commands. `--operator`
names who runs it in the worker's records (by default `kubectl auth whoami` and the local
account); `--context` picks the kubeconfig context. Step by step, `checkpoint-job --plan
plan.json --claim CLAIM [--node NODE] --output job.json` renders one Job from a plan.

The operator needs the rights in [preparation, step 13](KUBERNETES-PREP.md#13-workspace-checkpoints-and-cleanup-qualification);
the worker and every bundle Pod keep none. Creating a Job in the sandbox namespace amounts to
creating Pods there, which can mount any Secret of that namespace, which is why no service
holds that right: a later checkpoint controller would need its own service account and an
admission policy that pins what its Jobs may contain.

Not built yet: checkpoints of running sandboxes without suspending them (Longhorn clones,
`clone_pvc`), and of volumes whose environment the worker no longer binds (a static claim on
a Released PV).

### Restoring a checkpoint

A kept checkpoint is restored into a **fresh environment** of its project: one the worker
creates for the restore, never one that already holds research. The target is either a new
environment ID (a drill, or a copy to look at), or the ID of an environment the worker
records as deleted, such as a session's environment lost with its volume: the worker creates
it again under the same ID with a new sandbox, and the session finds its files back on its
next use. A restore never writes into an existing environment, so it cannot overwrite work.

1. **Plan** (the worker, `restore-plan`): the checkpoint must be `kept`; the worker reads
   the kept copy back in full against its recorded receipt (a damaged copy is refused before
   anything is created) and computes its **tree digest**: one line per path in byte order
   with its kind, mode, size, content SHA-256 or link text and hardlink group, which does
   not depend on the order a disk lists a directory in (set-user-ID and set-group-ID bits,
   which a restore never writes, are left out of it); a checkpoint with a root entry named
   `.sandbox-science-restore` is refused. It creates the target through its own API with
   the checkpoint's project, profile and policy revision (only a new environment counts: an
   existing one returned instead is refused; the project's current policy replaces the
   revision on first use, as for any environment), records the restore, which
   **holds the target's sandbox stopped with no expiry**, suspends it, confirms OpenShell
   says `Stopped`, and checks that nothing has run in it: no prefix identity, no package
   revision, no execution since the plan began. If that check fails nothing was written
   yet, so the restore is cancelled and the hold goes with it. While the restore is
   unfinished, the worker also refuses to delete the target (except through the restore's
   discard), a checkpoint plan of it, and the deletion or retirement of the checkpoint it
   reads.
2. **The Job** (`restore-job`, created by the operator): first the worker gives the attempt
   a nonce (`restore-attempt`, journaled); only the latest attempt's report, with no failure
   recorded after it, is ever verified. The Job runs `sandbox-science-worker checkpoint
   restore-write` on the trusted worker image, hardened as the checkpoint Job (no
   service-account token, no service links, no `fsGroup`, read-only root, no capabilities,
   `RuntimeDefault` seccomp, pinned to the volume's node), as `restore_run_as_user` (1000
   by default, the research images' sandbox user; it refuses a volume whose root another
   user owns, so a wrong setting fails before anything is removed), with the
   checkpoint Jobs' labels so the same NetworkPolicy gives it DNS and the store only. It
   mounts the target's claim **read-write** and the **restore key**'s Secret, read-only on
   the kept bucket; it never sees the upload bucket. It refuses a volume that is neither
   OpenShell's seed of a fresh workspace (the `.openshell-initialized` sentinel and no prefix
   identity) nor its own restore's earlier attempt (a `.sandbox-science-restore` marker with
   the restore's ID, written first), unless the volume already holds exactly the plan's tree
   (an earlier attempt finished writing; it then only reports it). It then empties the
   volume, writes the checkpoint from the kept bucket, checking every chunk's hash and length
   before a byte of it is written, never setting set-user-ID or set-group-ID bits, checks
   the archive's counts and the prefix identity, walks the volume back from disk to compute
   its tree digest, removes the marker and prints `restore_tree=<sha256>
   restore_attempt=<nonce>`.
3. **Verify** (the worker, `restore-verify`): the report must carry the current attempt's
   nonce, the Job's digest must equal the one the worker computed from the kept copy, the
   target must still be the stopped, fresh sandbox it created, and only then is the restore
   `restored` and the sandbox free to start. Its first
   executor adopts the restored prefix identity as the environment's baseline. A mismatch
   records the restore `failed`, which keeps the target held.

```sh
sandbox-science-kubernetes restore --config private.toml --checkpoint MANIFEST --target ENV   # prints the steps
sandbox-science-kubernetes restore --config private.toml --checkpoint MANIFEST --target ENV \
  --restore-key site-output/secrets/checkpoint-restore --apply
sandbox-science-kubernetes restore --config private.toml --resume RESTORE_ID \
  --restore-key site-output/secrets/checkpoint-restore --apply                         # after a failure
sandbox-science-kubernetes restore --config private.toml --discard RESTORE_ID --apply    # give the target up
```

With `--apply` it runs the plan (`kubectl exec` into the pool's worker container, under the
worker's read-only key), finds the target's claim and node (refusing while a Pod mounts it),
gets the attempt's nonce, creates the restore Secret from `--restore-key` (the directory
`site` writes, with `credentials` and `config`), creates the Job, journals it, waits, reads
the digest from its log, deletes the Secret and the Job and has the worker verify the
digest, printing the Job's log. The restore key reads every project's kept checkpoints and
the sandbox namespace runs untrusted sandboxes, so by default its Secret exists only while a
restore Job runs; `--standing-secret` uses one the operator keeps there instead. A Secret
the command cannot delete is reported, to be deleted by hand. A failure prints the Job's
log, deletes the Job and the Secret, journals the failure and leaves the target held:
`--resume` rechecks that the target is still the stopped, fresh sandbox, then runs another
Job attempt (it empties the volume again, since the marker says it is that restore's, or
only reports a volume that already holds the tree), and `--discard` ends the restore after a
typed confirmation, deleting the target environment only if it is still bound to the sandbox
the restore created (one created again since, or gone, is left alone, so a stale restore
never blocks an ID). Nothing in a restore needs the keeper key, so the operator's machine
needs no S3 SDK for it; the bundle needs `restore_secret` (`site` writes the key's files from
the site file's `restore_credentials` and `restore_config`). The operator's rights are those
of a checkpoint plus creating and deleting Secrets in the sandbox namespace
([preparation, step 13](KUBERNETES-PREP.md#13-workspace-checkpoints-and-cleanup-qualification)).

What a restore does not do: it brings back the workspace's files, installed packages and
prefix, not kernels' memory, the control store's runs and documents, or a deleted session
(the control store must still name the environment for a session to use it); timestamps,
ownership beyond the sandbox user, extended attributes and special files are outside the
checkpoint format. Run it while no session uses the target's ID: a session that creates the
environment first wins, and the plan then refuses. The target's volume must be large enough
for the checkpoint (the gateway's default workspace size); a full volume fails the Job by
name. While a restore is unfinished, a checkpoint of its target is refused and cleanup
refuses its volume (its environment is live).

The worker journals each restore in `checkpoints.sqlite3`'s append-only operations journal
with who ran each step: `intent` (with the checkpoint and the target), `attempt` (its
nonce), `job` (its name, claim, node and attempt), `retry`, `verified` or `failed`,
`discarding` and `cancelled`.

After a restore that created a deleted environment's ID again, the old sandbox's kept
checkpoints are listed for retirement like a deleted environment's (`--retire-checkpoints
ENV`): the environment is bound to another sandbox now, so their workspace is gone from it.

As seen on the first cluster (2026-10-28):

* an environment the worker has created but never run anything in has no recorded prefix
  baseline yet, and the plan refuses it ("checkpoint required prefix has no known retained
  baseline"); its first execution records the baseline;
* a finished checkpoint Job's Pod still names the workspace claim, and Kubernetes keeps a
  claim that a Pod names (`kubernetes.io/pvc-protection`), so deleting the environment
  while the Job was kept (a week then) left its claim `Terminating` and the volume `Bound`.
  Since then `checkpoint` deletes its Job once the worker verified it, keeping its log in
  the operator's output, a Job left behind goes `job_ttl_seconds` after it ends, and cleanup
  deletes a finished checkpoint Job that still names a claim before the claim itself;
* OpenShell names a sandbox's Kubernetes objects after its workspace: the `Sandbox` and the
  claim's owner are `default--ss-…` and the claim `workspace-default--ss-…`, while the
  worker records `ss-…`. Cleanup compared them as they were, so a claim that still existed
  showed "owner unknown; no verified checkpoint" and its checkpoints "volume gone"; it now
  drops the workspace first (below).

## Cleanup

`sandbox-science-kubernetes cleanup --config private.toml` lists, without changing anything:

- **workspace volumes left behind by deleted sandboxes**: Released PVs whose claim was in the
  sandbox namespace, and claims whose owning `Sandbox` is gone, each with size, age, phase,
  owner (project, session and environment, from the worker's records, deleted ones
  included) and how many verified checkpoints it has;
- **checkpoints beyond the kept count**: kept checkpoints of a workspace older than its
  latest `keep`, and deletions that began and did not finish;
- **retirable checkpoints**: the latest `keep` checkpoints of an environment the worker
  records as deleted, or bound to another sandbox since a restore created it again, which
  no candidate list ever offers. Each is `retirable
  (--retire-checkpoints ENV)` once its volume is gone and nothing references it, otherwise
  `REFUSED retirement` with the reasons.

Owners are matched exactly by the worker's sandbox name. OpenShell names a sandbox's
Kubernetes objects `<OpenShell workspace>--<sandbox>` (the `Sandbox` and the claim's owner
reference; the claim `workspace-<workspace>--<sandbox>`), and the worker's names never
contain `--`, so cleanup takes what follows the last `--` (`worker_sandbox` in
`kubernetes_operations.py`, the rule of `new_sandbox_name` in the worker), for any
workspace name. A Released PV whose claim is gone is traced by its claim's name the same
way, and only to a sandbox the worker records or that exists.

Each line says `candidate` or `REFUSED` with the reasons. Then it deletes only the items
named on its command line, with `--apply`, after a typed confirmation (`--yes` skips it):

```sh
sandbox-science-kubernetes cleanup --config private.toml --volume PV --checkpoint MANIFEST     # prints the commands
AWS_PROFILE=checkpoint-keeper sandbox-science-kubernetes cleanup --config private.toml \
  --volume PV --checkpoint MANIFEST --keeper-kept operator/checkpoint-kept.toml --apply
AWS_PROFILE=checkpoint-keeper sandbox-science-kubernetes cleanup --config private.toml \
  --retire-checkpoints ENV --keeper-kept operator/checkpoint-kept.toml --apply
AWS_PROFILE=checkpoint-keeper sandbox-science-kubernetes cleanup --config private.toml \
  --expire-uploads 168 --keeper-upload operator/checkpoint-upload.toml --apply
```

The keeper key's steps (deleting checkpoint objects, expiring uploads) run
`sandbox-science-worker` on the operator's machine, whose environment needs the S3 SDK
(`uv sync --extra s3` in the checkout); with `--keeper-kept` or `--keeper-upload` given,
cleanup first asks that program (`checkpoint sdk-check`) and refuses before anything runs
when boto3 is missing.

Refusals, checked against a fresh survey and again by the worker:

- a volume whose sandbox exists, which a Pod mounts, which the worker records for a live
  or paused environment, a hand-back under way or retained, or a checkpoint hold;
- a claim that a checkpoint Job still running, or any Job that is not a checkpoint Job,
  names (the line names the Job). A *finished* checkpoint Job that names it is listed
  with the volume and deleted first, with the UID the survey saw as a precondition
  (`kubectl delete --raw …/jobs/<job> -f -`, propagation `Background`): its Pod would
  otherwise keep the claim `Terminating`;
- a volume or claim annotated `sandbox-science.io/keep` (with a reason as its value): the
  operator's mark for a volume a recovery set still needs;
- a volume without a current verified checkpoint, unless `--without-checkpoint VOLUME`
  names it (repeatable; it covers only the volumes it names): none, or the newest predates
  the last time the worker started its sandbox (recorded from this version on, at each
  resume; the start at creation is not recorded), since work after a checkpoint would be lost.
  The listing shows the newest checkpoint's age and the last start;
- the last verified checkpoint of a workspace whose volume still exists, and any checkpoint
  that is not a candidate. Only checkpoints beyond a workspace's latest `keep` are
  candidates for `--checkpoint`, which refuses a retirable one;
- retiring, `--retire-checkpoints ENVIRONMENT` (repeatable; all of that environment's
  remaining kept checkpoints or none), unless the worker records the environment as
  deleted or bound to another sandbox than the checkpoints', its volume is gone (no PV or
  claim of its sandbox, or a PV already being deleted with reclaim policy Delete) and
  nothing references it (no hand-back, no hold, no unfinished restore reading it); a
  checkpoint an unfinished restore reads is never deleted either. The worker
  checks the same records again (`checkpoint forget --volume-gone --retire ENV`) and
  journals each intent with the environment it retires (`"retire"` in the detail); the
  deletion then runs as any checkpoint's below.

A volume's deletion: the worker journals the intent (refusing it if its records forbid it),
finished checkpoint Jobs that name the claim are deleted (each by its UID), the PV's reclaim
policy becomes Delete so its provisioner frees the storage (a JSON patch that first tests
the claim UID and phase the survey saw), the claim is deleted if it remains
(with its UID as a precondition), and the worker journals it done; a failed step is
journaled as failed and stops the run. A checkpoint's deletion: the worker marks it
`deleting` (it stops counting at once) and journals the intent; the chunk list the worker
recorded at verification is copied to a private directory of the operator's; the keeper key
removes the chunks no other kept manifest names and then, last, the manifest, a missing
object counting as removed; the worker journals it done. A failure leaves it `deleting`,
listed again next time, and the next run finishes it even if the manifest is already gone. Every journal row names who ran it and is
never changed or removed (`checkpoints.sqlite3`, beside the worker's state). Run cleanup
when no checkpoint is being promoted: a chunk two checkpoints share is kept only if the other
manifest is already in the kept bucket.

### Upload expiry

The upload bucket keeps what checkpoint Jobs wrote until something removes it: uploads that
were promoted (the kept bucket has its own copy) and uploads that never were (a Job whose
checkpoint failed, a run interrupted before promotion). `--expire-uploads HOURS` (1 to 8760;
a week, 168, is a sensible value) removes the upload bucket's objects older than that which
no unpromoted checkpoint needs, and never touches the kept bucket:

1. the worker journals the intent (`upload-expiry`, with the age) and refuses while any
   checkpoint hold is active or another expiry is open, since a capture under way may reuse
   an old chunk without rewriting it; it prints what it protects: the manifest and recorded
   chunks of every checkpoint it verified in the upload bucket and that is not yet kept.
   Until the expiry's end is journaled (at most 6 hours), the worker refuses new checkpoint
   plans and upload verifications, so nothing can come to need an object the expiry
   removes;
2. the keeper key (`sandbox-science-worker checkpoint expire-objects`, locally) lists the
   upload bucket with each object's time and deletes the older unprotected ones, manifests
   first, so no manifest is left naming a removed chunk; an object whose time the store
   does not give is kept; it prints how many it removed and kept, and stops within 5 hours;
3. the worker journals it done, with those counts, or failed if the deletion failed; an end
   without its open intent is refused.

A bucket holding only leftovers, with no checkpoint under way, is emptied of everything
older than the age except the uploads of checkpoints verified and never promoted: those stay
until they are promoted (the `promote` and `verify --stage kept` commands that `checkpoint`
prints when it stops before promotion; `inventory` lists them as `verified`). The keeper
key needs write (which in Garage includes delete) on the upload bucket for this
([preparation, step 6](KUBERNETES-PREP.md#6-garage-artifacts)). This replaces a Garage
lifecycle rule, which Garage supports (`Expiration` only) but which needs an owner key and
expires by age alone: a chunk a recent checkpoint reused keeps its old time and would go
before that checkpoint is promoted.

## Two instances on one cluster

D94 gives each trust group a complete instance: its own bundle, normally its own namespace.
The renderer is shown to keep two bundles apart (tests render pairs with different `name`s in
one namespace, the same `name` in two namespaces, and names that are prefixes of each other,
with and without ingress): every object name is `<name>-<role>`, so two names never produce
one object (`name` is at most 40 characters); every Pod, Deployment selector, Service
selector and NetworkPolicy carries and selects `app.kubernetes.io/part-of: <name>`, so the
default-deny, the application, pool and manager policies and the ingress policy select only
their own instance's Pods, and an in-namespace peer is always the same instance's Pods; the
PVCs, the pool ConfigMap, the TLS Secrets and the Certificates are named by instance. Two
bundles with the same `name` in different namespaces differ only by namespace, which
Kubernetes separates.

What the renderer cannot make distinct, the operator does, per instance:

| Per instance | Why |
| --- | --- |
| Namespace (recommended), `name`, the Secrets named under `[secrets]` | The bundle references Secrets by name and cannot see another bundle's; two instances naming one Secret share its credentials. Separate namespaces also keep Secret access and the pull secret apart |
| `ingress.workspace_host` and `ingress.preview_host`, and an Authentik application and forward-auth `Middleware` | Host names are cluster-wide: two routes with one name make Traefik pick one. The Authentik application's group decides who may log in |
| An OpenShell workspace on the shared gateway and the instance's own CLI client identity (`runtime_manager.openshell_gateway` and the profile keys in its Secret) | One OpenShell workspace per user (D94) keeps one instance's sandboxes and policies from the other's |
| Garage buckets and keys (`<instance>-artifacts`, `-checkpoint-upload`, `-checkpoints`; keys `-control`, `-workspace`, `-checkpoint-upload`, `-checkpoint-verify`, `-checkpoint-keeper`) | A bucket and key per user ([preparation, step 6](KUBERNETES-PREP.md#6-garage-artifacts)) |
| A LiteLLM key and budget (the runtime manager's `model.key` and the keys control's settings use) | The model gateway enforces each instance's budget and shows its spend by key |
| Worker tokens, CAs and certificates for the `<name>-worker`, `-control` and `-middleware` Service names | Certificates cover one instance's Service DNS names |

One limit is the network's. `network.runtimes` admits the supervisors of one gateway
(`openshell.ai/gateway-id`) to control's port 8443, so two instances on one gateway whose
sandboxes share a namespace admit each other's supervisors at the network; only the
runtime token (KRB-6) then decides which connection control accepts. Give each instance its
own sandbox namespace (and select it in `network.runtimes`) where OpenShell allows it.

## Log collection

The first deployment collects every service's logs into Loki through Alloy (D103). The bundle renders nothing for Loki or Alloy: the operator installs both ([preparation, step 11](KUBERNETES-PREP.md#11-log-collection)). The bundle makes the logs collectable.

- **JSON on the container's output.** Every container sets `SS_LOG_FORMAT=json`, so each service writes one JSON object per line on standard error, which the kubelet keeps with standard output as the container's log. The shared setup is `sandbox_science.contracts.logs`: `ts` (UTC), `level`, `service`, `logger`, `msg`, `event` when the call names one, and, when known, `project`, `session`, `run`, `environment` and `request`; an exception is one `exc` field. A message, field or traceback passes the redaction first (URL user information, `Authorization` and `Bearer` values, `name=value` where the name ends in `token`, `secret`, `password`, `api_key`, `credential` or `cookie`, and a few credential formats), and a field named like a secret is never written. Local runs keep the text line unless `SS_LOG_FORMAT=json` is set ([the unit](UNIT.md#how-it-runs)).
- **`service`** is `control`, `workspace`, `harness`, `agent-runtime`, `worker`, `middleware`, `cache`, `runtime-manager` or `unit` (`mcp-proxy` in older lines, before the proxy retired). In the pool Pod each container runs one service of the unit (`--only`), so a line's `service` is the container's; a line a service wrote that is not a record (a traceback, a library's output) becomes a record of its service. The runtime manager passes on the agent runtime's output as records of the service `runtime` when the runtime wrote none.
- **Pod labels.** Every Pod the renderer makes (`app`, `pool`, `runtime-manager`) carries `app.kubernetes.io/name: sandbox-science`, `app.kubernetes.io/instance: <name>` (the bundle's `name`), `app.kubernetes.io/part-of: <name>` and `app.kubernetes.io/component: app|pool|runtime-manager`; the container name (`control`, `workspace`, `worker`, `middleware`, `cache`, `manager`) tells the service apart within a Pod. The log labels are on the Pods and objects only: selectors and NetworkPolicies keep to `part-of` and `component`. Alloy reads logs through the Kubernetes API or the nodes' files, so no NetworkPolicy needs an ingress rule for it.
- **OpenShell's events.** The gateway's and the sandbox supervisors' OCSF events are OpenShell's to export, from its own configuration: `ocsf_json_enabled` makes the supervisors write `OCSF-JSON` lines on their Pod's standard error and the gateway write JSONL (`[openshell.gateway.ocsf_log]`), and `[openshell.gateway.otlp]` with `[openshell.gateway.otlp.ocsf]` sends the events, and the traces, to an OTLP/gRPC collector that the sandboxes can reach too. OpenShell's pages on [observability](https://github.com/NVIDIA/OpenShell/blob/v0.1.5/docs/observability/overview.mdx), [OCSF JSON export](https://github.com/NVIDIA/OpenShell/blob/v0.1.5/docs/observability/ocsf-json-export.mdx) and [telemetry](https://github.com/NVIDIA/OpenShell/blob/v0.1.5/docs/observability/telemetry.mdx) (its anonymous usage data, separate from these signals) name every setting. The gateway's Prometheus listener (`metrics_bind_address`) is scraped, not logged.
- **A sample Alloy configuration** is [alloy-config.alloy](../examples/kubernetes/alloy-config.alloy): the bundle's Pods selected by `app.kubernetes.io/name`, the JSON fields `level` and `service` as labels and the identifiers as structured metadata (a project, session, run or request is a filter, not a stream), an optional pipeline for the supervisors' `OCSF-JSON` lines, and an optional OTLP receiver. It has not run on a cluster. Retention, the Loki URL and Alloy's own access are the operator's.

## Alert rules

The first alerts over the structured logs (D103), six rules, are rendered only with an `[alerts]` section: one ConfigMap, `<name>-alert-rules`, whose key `sandbox-science-alerts.yaml` is a rule file in the Prometheus rule format that Loki's ruler reads, with LogQL expressions. Without the section, nothing is rendered. How to load it into the ruler is [preparation, step 11](KUBERNETES-PREP.md#11-log-collection).

```toml
[alerts]
# namespace = "monitoring"          # where the ConfigMap goes (Loki's); by default the bundle's
# tenant = "fake"                   # the Loki tenant: "fake" for a single-tenant Loki
# configmap_labels = { loki_rule = "true" }   # what Loki's rules sidecar selects (the default)
# error_threshold = 10              # errors from one service within 5 minutes that alert
# canary_window_minutes = 20        # every canary in it failed: longer than the canaries' interval
```

The ConfigMap carries `configmap_labels` and the annotation `k8s-sidecar-target-directory: /rules/<tenant>`, the directory per tenant the ruler reads. Every rule selects only this instance's lines, `{namespace="<namespace>", instance="<name>"}`, the labels the [sample Alloy configuration](../examples/kubernetes/alloy-config.alloy) sets, and finds an event by its compact JSON text (`` |= `"event":"…"` ``), so the rule does not depend on how a pipeline labels `event`. `project` and `environment` come from the lines' structured metadata, as the sample keeps them; the other fields are parsed from the line (`| json kind="kind"`). Each alert has the labels `severity`, `namespace` and `instance`, and those it is grouped by:

| Alert | Fires when | Grouped by |
| --- | --- | --- |
| `SandboxScienceServiceErrors` | One service logged more than `error_threshold` records at `error` or `critical` within 5 minutes | `service` |
| `SandboxScienceManagerUnreachable` (critical) | The runtime manager has noted `gateway_unreachable` or `control_unreachable` for over 10 minutes: it logs `event: runtime_manager_note` with the note's `kind` and `since` when the note starts and every 5 minutes while it holds, and the rule asks for a record within each 8 minutes, held for 10 (`for: 10m`). It resolves up to 8 minutes after the note clears | `service`, `kind` |
| `SandboxScienceCreationUnknown` | The worker's reconciliation read a creation with an unknown outcome past `creation_grace` and could not yet confirm the sandbox present or absent: it logs `event: environment_creation_unknown` at each pass (every `creation_recheck`, 30 s by default), with the environment and project. The window is 15 minutes, longer than `creation_recheck`, so a creation unknown at every pass stays in it from one pass to the next; with a `creation_recheck` over 15 minutes, widen it. A creation it settles is `environment_creation_reconciled` instead | `service`, `project`, `environment` |
| `SandboxScienceMcpCanaryChanged` | A scheduled [MCP canary](MCP-CONNECTORS.md#canaries) changed from `listed` (or no earlier result) to `unavailable`, `denied` or `timeout` (`event: mcp_canary_changed`): the notification. It resolves 20 minutes after the change | `service`, `server`, `status` |
| `SandboxScienceMcpCanaryFailing` | Every canary of a server within `canary_window_minutes` (20) failed (`event: mcp_canary`, `status` not `listed`, and no listed one): it holds while the server keeps failing and resolves at its first listed result. The window must exceed the canaries' interval (15 minutes by default). With `[mcp_direct]` the bundle's control runs the canaries (`[mcp_direct.canaries]`): both canary rules fire from their logs where Alloy collects them | `service`, `server` |
| `SandboxSciencePolicyHold` | A detect-and-hold rule opened a hold: the middleware's `event: detect_finding` with `outcome` `hold` (a session's outward calls held, [detect and hold](POLICY-MIDDLEWARE.md#detect-and-hold-d103)) or `hold_call` (one call held for approval). Not grouped by project: a held call's record, and an HTTP detection's, carry none | `service`, `rule`, `outcome` |

**Network-policy drops are not alerted on.** The services' logs do not carry them: a Kubernetes NetworkPolicy drop is the CNI's to report, and OpenShell's sandbox network denials reach Loki only as OCSF events through the sample's optional OCSF pipeline, whose `class_uid` and `action` values have not been seen on a cluster. The rule file ends with that rule commented out, for once they have. None of the rules has run in a Loki ruler yet.

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
is the teardown. `request forget` clears a record whose create never happened (no ID,
and the gateway answers that the name is absent); it is the operator's decision. When
the gateway rolls a create back (the create returned, then the sandbox vanished, as
OpenShell does with one not ready within its bootstrap grace), the request's answer
and `request status` say so, the record carries `outcome: create_rolled_back` with the
gateway's reason when it gave one, and the next `request bootstrap` clears that record
itself, once the gateway confirms the name absent, and creates a new sandbox: no
`forget` first. Deleting the manager Pod stops the runtime without deleting its
sandbox.

The manager waits for what it cannot reach instead of exiting. If the OpenShell gateway
does not answer, at start or later, it keeps running, takes no automatic action and
uses none of its ten recovery attempts, and asks the gateway again after 5 s, doubling
to 60 s, until it answers; then it reconnects as usual. It exits (status 1, with
`error: …` in its log) only on bad settings, an unusable state directory, another
OpenShell release or a second manager on the same volume.

`request status` shows the serving process under `manager`. Its `note` says what needs
attention now, and only now: `note_kind` is `blocked` (automatic action needs the
operator), `gateway_unreachable`, `control_unreachable` or `action_failed`, the first
of these that holds, and `note_since` when it began. A note clears itself when its
condition ends, such as control or the gateway answering again or an action that
succeeds after a failed one. A failed request's answer, and the note, name what failed:
for an OpenShell command, its name, exit code and the last line it wrote (`openshell
sandbox create failed (exit 1): …`, or `no error text from openshell`). The `orchestrator` image's OCI digest is distinct from its source revision,
which must match `control.json`'s `image_revision`.

Do not run `sandbox-science up` against this bundle as a bootstrap workaround: it
creates a local control instance, rotates generation/token and creates a new
runtime, breaking the cluster's intended original identity. Likewise, a failed
handshake or empty PVC never authorizes inventing a replacement runtime or clearing
ownership/recovery state. The manager and the bundle are not yet qualified on a live
Kata cluster ([KRB-27](specs/kubernetes-runtime-bootstrap.md#delivery-and-tests)).
