# Offline Kubernetes deployment bundle

The `sandbox-science-kubernetes check|render` command prepares a deterministic JSON
Kubernetes List. It reads only its explicit, bounded site-private TOML file. It does
not read kubeconfig or referenced Secrets, call `kubectl`, apply resources, build or
publish images, or create an OpenShell runtime. No actual cluster or native ARM64
deployment is qualified by rendering or schema validation.

The trusted application image runs control and workspace in one Pod over one block
PVC. A second Pod runs worker and policy middleware over their shared block PVC;
package cache is optional there. Deployments use one replica and Recreate strategy,
retain PVCs independently of Deployment ownership, and rely on the existing service
state locks. All containers run as UID/GID 1000 with fsGroup 1000, no added Linux
capabilities, no privilege escalation, a read-only root filesystem and default
seccomp. No application service account token, RBAC, privileged Pod, host path,
runtime agent loop or research process is generated. Install and qualify OpenShell
separately with its own required gateway authority and isolation tier.

A first UID-1000 init container mounts only that Pod's data PVC and creates the
fixed missing state directories: `control` for the application, `worker` and
`middleware` for the pool, plus `package-cache` when enabled. This breaks the cold
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

Each of `control`, `workspace`, `worker`, `middleware` and optional `cache` accepts
these integer bounds; missing fields use the defaults below. Requests must fit
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
| `secrets.control` | `control.json`, `control-token`, `worker-token`, `control.crt`, `control.key`, `worker-ca.crt` |
| `secrets.workspace` | `worker-token`, `worker-ca.crt` |
| `secrets.worker` | `worker.toml`, `worker.crt`, `worker.key` |
| `secrets.middleware` | `middleware.toml`, `middleware.crt`, `middleware.key` |
| optional `secrets.cache` | `cache.toml` |

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
Default HOME is the existing per-container `/tmp` scratch volume. Engine/gateway
credentials belong only in the worker's trusted state/private settings, not in the
application containers. The renderer still does not mount kubeconfig or an engine
socket or grant cluster authority to any application.

Configure the actual service settings using the existing
[worker](WORKER.md), [control architecture](ARCHITECTURE.md) and
[policy middleware](POLICY-MIDDLEWARE.md) documentation. Important cluster bindings:

* `control.json`: `state_dir` is `/var/lib/sandbox-science/control`, `listen` is
  `0.0.0.0:8443`, `tls.certificate` is `/private/control.crt`, `tls.private_key`
  is `/private/control.key`. `runtime_token_file` points at `/private/control-token`.
  `worker` is `https://<name>-worker.<namespace>.svc:8100`, `worker_ca_file` is
  `/private/worker-ca.crt` and `worker_token_file` is `/private/worker-token`.
  Record the trusted runtime image revision, generation, permitted projects,
  model aliases/effort and other required settings; the renderer cannot infer them.
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
permit app-to-worker TCP 8100, approved `network.runtimes` to control TCP 8443 and
approved `network.supervisors` to middleware TCP 50051. Cache TCP 8090 requires both
`secrets.cache` and nonempty `network.sandboxes`. These are approved peer selectors,
not a promise that arbitrary OpenShell Pods have the template's labels.

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
executed by this tool. The operator must also prepare the exact project policies,
record ownership/generation and bootstrap the separate orchestrator runtime through
the existing trusted workflow; there is no automatic runtime controller here.

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
