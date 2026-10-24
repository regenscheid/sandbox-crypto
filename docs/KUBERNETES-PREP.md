# Cluster preparation checklist

For the operator of the first Kubernetes deployment ([D92, amended 2026-10-23](DECISIONS.md), [scope and proofs](KUBERNETES.md#first-deployment-scope-and-proofs); isolation from [D43](KUBERNETES.md#sandbox-isolation); logs from D103). The steps run in order; each says what to verify and how to back out. The commands and manifests are skeletons: private values (endpoints, node names, bucket names, keys) are placeholders and stay out of Git. Nothing here is qualified on a live cluster; the first run of a step may find that a command differs on your versions, which the verify line will show.

The cluster this describes: Talos 1.13 with the `kata-containers` extension (3.32.0) on the amd64 nodes, Kubernetes 1.36, Flannel with kube-network-policies, Longhorn v1.12, MetalLB, Traefik, cert-manager, Authentik, an in-cluster model gateway, and Garage S3 outside the cluster. Arm64 Raspberry Pi nodes host trusted pods only.

## Verify before use

This checklist was written from the repository, OpenShell 0.1.5's documentation and
source, and general Kubernetes practice, without access to the cluster. Confirm these
against the live tools before relying on them; each is marked where it appears:

- the OpenShell Helm chart's value keys, the `--version` form and the Agent Sandbox
  release to pin;
- the chart's CA issuer name and the fields of a client identity's `metadata.json`;
- Longhorn 1.12's backup-target and recurring-job resource shapes;
- the `garage` CLI commands;
- the bundle's container names (`control`, `workspace`, `worker`) and the exact proof
  commands in step 10, which were not run.

The bundle renders no `imagePullSecrets`; step 1 attaches the pull secret to the
namespace's default ServiceAccount instead.

## Placeholders

| Placeholder | Meaning |
| --- | --- |
| `<name>` | The bundle `name` (for example `research-workbench`); the Deployments are `<name>-app`, `<name>-pool` and `<name>-runtime-manager`, the PVCs `<name>-app-data`, `<name>-pool-data` and `<name>-runtime-manager-data` |
| `research` | The bundle's namespace (`namespace` in the bundle TOML) |
| `openshell` | The OpenShell gateway's namespace |
| `research-runtimes` | The sandbox namespace (`server.sandboxNamespace`); the bundle's `network.runtimes` and `network.supervisors` peers select it |
| `<gateway>` | The runtime manager's `openshell_gateway` name (CLI profile name) |
| `<kata-node>` | An amd64 node that runs Kata |

Tools on the operator's machine: `kubectl` (cluster-admin context), `talosctl`, `helm` 3, the OpenShell 0.1.5 CLI, and this repository's `uv` environment (`uv sync --extra s3`).

## Images

Pin every image by digest from the publish run of the manual image workflow ([images](IMAGES.md#arm64)); a tag is not a pin. All are under `ghcr.io/regenscheid/sandbox-crypto/` and private.

| Image | Used in |
| --- | --- |
| `trusted-app` | Bundle `images.app` (control, workspace and the runtime manager) |
| `trusted-worker` | Bundle `images.worker` (worker, middleware, optional cache) |
| `orchestrator` | The runtime manager's `runtime-manager.json` `image`, with its source revision matching `control.json`'s `image_revision` |
| `minimal`, `crypto-base` | The worker's `[openshell.environments] profiles` |
| `mcp-proxy` | The MCP proxy's own sandbox ([MCP connectors](MCP-CONNECTORS.md#running-it)) |

Verify: each digest resolves for both `linux/amd64` and `linux/arm64` (`docker buildx imagetools inspect <image>@sha256:...`). Record the six digests and the source revision in the private site file.

## 1. Namespaces and the pull secret

```sh
for ns in research openshell research-runtimes; do kubectl create namespace "$ns"; done

# One pull secret per namespace that pulls a private image. Read the token from a file or prompt, never from shell history.
for ns in research research-runtimes; do
  kubectl -n "$ns" create secret docker-registry ghcr-pull \
    --docker-server=ghcr.io --docker-username=<github-user> --docker-password-stdin < ghcr-token.txt
done

# The bundle renders no imagePullSecrets, so attach the secret to the default ServiceAccount that its Pods use.
kubectl -n research patch serviceaccount default -p '{"imagePullSecrets":[{"name":"ghcr-pull"}]}'
```

The sandbox images (`minimal`, `crypto-base`, `orchestrator`, `mcp-proxy`) are pulled in `research-runtimes`; the gateway's `server.sandboxImagePullSecrets` (step 5) names the same secret. The OpenShell gateway and supervisor images are public and need no secret.

Verify: `kubectl -n research get sa default -o jsonpath='{.imagePullSecrets}'`; a throwaway pod from the `trusted-app` digest in `research` pulls (delete it afterwards).

Rollback: `kubectl delete namespace` removes only empty namespaces you just created. Do not delete `research` once PVCs exist (step 2 sets Retain, but the data would be orphaned).

## 2. Storage

### 2.1 A Retain class for research volumes

The bundle needs block-backed SQLite-compatible storage; its PVCs are not to be deleted as shutdown, and the sandbox workspace PVCs must survive an Agent Sandbox cleanup ([storage lifecycle](KUBERNETES.md#storage-lifecycle)). All Longhorn classes on the cluster are `Delete`, so add one with `Retain` and the recurring jobs of 2.2.

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: research-retain
provisioner: driver.longhorn.io
reclaimPolicy: Retain
allowVolumeExpansion: true
volumeBindingMode: Immediate        # or WaitForFirstConsumer; the app, pool and manager Pods are pinned to a node architecture
parameters:
  numberOfReplicas: "2"             # per the nodes you can spare; 3 if three nodes have room
  staleReplicaTimeout: "30"
  fsType: ext4
  dataLocality: best-effort
  recurringJobSelector: '[{"name":"research-snapshot","isGroup":false},{"name":"research-backup","isGroup":false}]'
```

Use `research-retain` for: `app.storage_class`, `pool.storage_class`, `runtime_manager.storage_class` in the bundle TOML (the manager's PVC is `ReadWriteOncePod`; Longhorn supports it), and the gateway's `server.workspaceStorageClass` (step 5). Size: about 100 GiB for the app and 200 GiB for the pool ([D92](KUBERNETES.md#first-deployment-scope-and-proofs)), 1 GiB for the manager, and the workspace default per the `crypto-base` footprint (about 5.2 GB before headroom, so at least 20 Gi).

Verify: a 1 Gi test PVC reaches `Bound`; `kubectl get pv -o custom-columns=NAME:.metadata.name,RECLAIM:.spec.persistentVolumeReclaimPolicy` shows `Retain` for it. Delete the PVC, confirm the PV stays `Released`, then remove the PV and the Longhorn volume by hand.

### 2.2 Recurring snapshot and backup to Garage

Create a Garage bucket and key for backups, separate from the artifact bucket of step 6 (`garage bucket create longhorn-backup`, `garage key create longhorn-backup`, `garage bucket allow --read --write --owner longhorn-backup --key longhorn-backup`). Then the credential Secret and the backup target:

```sh
kubectl -n longhorn-system create secret generic garage-backup \
  --from-literal=AWS_ACCESS_KEY_ID=... --from-literal=AWS_SECRET_ACCESS_KEY=... \
  --from-literal=AWS_ENDPOINTS=https://<garage-s3-endpoint> \
  --from-file=AWS_CERT=<private-ca.pem>      # only if the endpoint certificate is from a private CA
```

Set the backup target to `s3://longhorn-backup@garage/` (the trailing `/` is required) with credential secret `garage-backup`, in the Longhorn UI under Backup and Restore, or through the Helm `defaultBackupStore` values. Garage needs path-style access: leave `VIRTUAL_HOSTED_STYLE` unset.

```yaml
apiVersion: longhorn.io/v1beta2
kind: RecurringJob
metadata: {name: research-snapshot, namespace: longhorn-system}
spec: {task: snapshot, cron: "0 */6 * * *", retain: 8, concurrency: 2}
---
apiVersion: longhorn.io/v1beta2
kind: RecurringJob
metadata: {name: research-backup, namespace: longhorn-system}
spec: {task: backup, cron: "30 2 * * *", retain: 14, concurrency: 1}
```

The schedule is a starting point (snapshots every six hours, one backup a night); tune it to the write rate and the Garage capacity. The class applies the jobs to volumes created from it, so create the bundle's PVCs after this step. A volume created earlier needs the jobs added to it in the Longhorn UI.

Verify: the target shows `Available` in Longhorn; after the first run, `kubectl -n longhorn-system get backups.longhorn.io` lists a backup per volume; restore one into a scratch volume and mount it before relying on it (the restore drill belongs in the backup runbook, backlog). A backup is crash-consistent, not application-consistent: for SQLite it relies on the database's journal. Take a manual snapshot before each migration or image change.

Rollback: remove the jobs with `kubectl delete recurringjob`; backups already in Garage stay until you delete them in Longhorn. Never delete a backup bucket that holds the only copy.

## 3. Kata on the amd64 nodes

```sh
talosctl -n <kata-node> get extensions                 # kata-containers 3.32.0 listed
talosctl -n <kata-node> ls /dev/kvm                    # KVM present (VM nodes need nested virtualization)
talosctl -n <kata-node> read /sys/kernel/security/lsm  # informational: Landlock on the host kernel
kubectl get nodes -L kubernetes.io/arch -o wide
```

Label the Kata-capable amd64 nodes (the label must match the RuntimeClass `scheduling.nodeSelector`), and the large node for the `crypto-base` profile's selector:

```sh
kubectl label node <kata-node> example.org/kata-capable=true
kubectl label node <large-node> research.example.org/capacity=large
```

Apply [the RuntimeClass](../examples/kubernetes/runtimeclass-kata.yaml) after replacing its placeholder node label and checking `overhead.podFixed` against what Kata uses on these nodes. It must carry `openshell.ai/sandbox-attachable: "true"`, or OpenShell refuses it at sandbox creation.

```sh
kubectl apply -f examples/kubernetes/runtimeclass-kata.yaml   # a copy with your label
kubectl get runtimeclass kata -o jsonpath='{.metadata.labels}{"\n"}{.handler}{"\n"}'
```

Smoke pod (pull the `minimal` image by its digest; any amd64 image with `uname` works):

```yaml
apiVersion: v1
kind: Pod
metadata: {name: kata-smoke, namespace: research-runtimes}
spec:
  runtimeClassName: kata
  restartPolicy: Never
  nodeSelector: {kubernetes.io/arch: amd64, example.org/kata-capable: "true"}
  imagePullSecrets: [{name: ghcr-pull}]
  containers:
    - name: smoke
      image: ghcr.io/regenscheid/sandbox-crypto/minimal@sha256:<digest>
      command: ["sh", "-c", "uname -r; grep -m1 hypervisor /proc/cpuinfo >/dev/null && echo hypervisor-flag"]
```

Verify: `kubectl -n research-runtimes logs kata-smoke` prints a kernel release that does not end in `-talos` (a Kata guest kernel), and `hypervisor-flag`; the Pod's node is `<kata-node>`. Repeat on every Kata node. A `-talos` release means the pod ran on plain runc: stop and fix the handler or the extension before going on. The Cloud Hypervisor handler `kata` is the only one on Talos 1.13. This host-side check does not prove the guest has the Landlock ABI 3 and seccomp notification that OpenShell requires; the first sandbox in step 9 does, because OpenShell fails closed without them ([isolation](KUBERNETES.md#sandbox-isolation)).

Rollback: `kubectl delete pod kata-smoke`; `kubectl delete runtimeclass kata` (no sandbox may exist); `kubectl label node <node> example.org/kata-capable-` removes a label.

## 4. The Agent Sandbox controller

OpenShell 0.1.5 needs the Agent Sandbox controller and its CRDs before the chart; the chart installs neither ([OpenShell setup](https://docs.nvidia.com/openshell/kubernetes/setup)). Pin a release rather than `latest`:

```sh
kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/<release>/sandbox.yaml
kubectl -n agent-sandbox-system get pods
kubectl get crd sandboxes.agents.x-k8s.io
```

Verify: the controller Pod is `Running` and the CRD is `Established`. Record the release. The controller image must run on the node it lands on (the cluster mixes arm64 and amd64 nodes; check the manifest's image platforms). After any in-place upgrade of Agent Sandbox, restart the OpenShell gateway so it detects the served API again.

Rollback: `kubectl delete -f` the same manifest, only while no `Sandbox` exists: deleting the CRD deletes every Sandbox object and may garbage-collect its workspace PVC.

## 5. OpenShell 0.1.5

The cluster's CNI must enforce ingress and egress NetworkPolicy in every sandbox namespace (Flannel with kube-network-policies does; step 10 proves it). Use cert-manager for the chart's PKI so you can issue client certificates from its CA ([managing certificates](https://docs.nvidia.com/openshell/kubernetes/managing-certificates)). The values carry [the gateway fragment](../examples/kubernetes/gateway-driver.toml):

```yaml
# openshell-values.yaml (private)
certManager: {enabled: true}
global:
  image: {tag: "0.1.5"}               # one release for gateway, supervisor and sandbox runtime
server:
  sandboxNamespace: research-runtimes
  sandboxImagePullSecrets: [{name: ghcr-pull}]
  workspaceDefaultStorageSize: 20Gi
  workspaceStorageClass: research-retain
  defaultRuntimeClassName: kata
  drivers:
    kubernetes:
      allowDriverConfig: true
      resourceAdmission: {enabled: true}
```

```sh
helm show chart oci://ghcr.io/nvidia/openshell/helm-chart --version 0.1.5     # confirm appVersion
helm template openshell oci://ghcr.io/nvidia/openshell/helm-chart --version 0.1.5 -n openshell \
  -f openshell-values.yaml --set agentSandbox.preflight.enabled=false | grep -n -i 'allow_driver_config\|runtime_class\|resource_admission'
helm upgrade --install openshell oci://ghcr.io/nvidia/openshell/helm-chart --version 0.1.5 \
  -n openshell -f openshell-values.yaml
kubectl -n openshell rollout status statefulset/openshell
```

If the chart rejects a key, the equivalent is `gatewayConfig` (the chart renders it into `gateway.toml`): `gatewayConfig: {openshell.drivers.kubernetes: {allow_driver_config: true, default_runtime_class_name: kata, resource_admission: {enabled: true}}}`. Whichever form, the rendered `gateway.toml` must show `allow_driver_config = true`, `default_runtime_class_name = "kata"` and resource admission enabled, as in the example fragment. `server.workspaceStorageClass` and the size are creation defaults for each sandbox's PVC. Keep the Helm values and the chart version with the site file.

The gateway's `gateway-id` (default: the chart's fullname, `openshell`) is the label value that the bundle's `network.runtimes` peer must name: `openshell.ai/component = "supervisor"` and `openshell.ai/gateway-id = "<id>"`. Confirm it after the first sandbox (step 9).

### 5.1 The gateway CA and two client identities

The runtime manager and the worker each hold their own OpenShell CLI profile with a client certificate of their own. Without OIDC every certificate that chains to the gateway's client CA is platform admin on the gateway, and there is no revocation list; revoking one means replacing the CA ([R1](specs/kubernetes-runtime-bootstrap.md#decisions-2026-10-22)). Issue each with a distinct common name and no `operator` organizational unit:

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata: {name: ss-manager-client, namespace: openshell}      # and ss-worker-client
spec:
  secretName: ss-manager-client-tls                            # and ss-worker-client-tls
  commonName: sandbox-science-manager                          # and sandbox-science-worker
  usages: [client auth]
  duration: 8760h
  renewBefore: 720h
  privateKey: {algorithm: ECDSA, size: 256}
  issuerRef: {name: <the chart's CA issuer>, kind: Issuer}     # kubectl -n openshell get issuer
```

A renewed certificate changes the Secret, and the bundle copies Secret files only at Pod start (changed credentials need an intentional Pod recreation), so renew on a date you choose and recreate the Pod then.

Assemble one CLI profile per identity, with the keys the bundle expects ([Secrets](KUBERNETES-BUNDLE.md#prepare-existing-secrets-and-recovery-state)):

| Key | Content |
| --- | --- |
| `active-gateway` | exactly `<gateway>` |
| `gateway-metadata.json` | `{"name":"<gateway>","gateway_endpoint":"https://openshell.openshell.svc.cluster.local:8080","is_remote":false,"gateway_port":8080,"auth_mode":"mtls"}` |
| `gateway-ca.crt` | the `ca.crt` of the client Secret (`kubectl -n openshell get secret openshell-client-tls -o jsonpath='{.data.ca\.crt}' \| base64 -d`) |
| `gateway-tls.crt`, `gateway-tls.key` | the identity's own `tls.crt` and `tls.key` |

The endpoint name must be one the gateway certificate covers (the chart's internal SANs cover `*.svc.cluster.local`; confirm the Service name with `kubectl -n openshell get svc`, and add `certManager.serverDnsNames` if you use another). Keep the files on the operator's machine only until the Secrets are created (step 7). The manager's files go into its Secret (step 7); the worker's profile is not expanded from Secret keys, so its tree is placed on the pool PVC (step 7).

Verify, from the operator's machine with the chart's own admin client bundle over `kubectl -n openshell port-forward svc/openshell 8080:8080`: `openshell status` shows `Connected` and `Authentication` not `Failed`. Test each new identity the same way against a copy of its profile. Both must authenticate.

Rollback: `helm uninstall openshell -n openshell` leaves the PVCs of existing sandboxes and the Agent Sandbox objects; delete sandboxes through `openshell sandbox delete` first. Removing a client identity means replacing the client CA, which invalidates every identity.

## 6. Garage artifacts

On the Garage host, one bucket and distinct keys per application (D94), with the artifact bucket separate from the Longhorn backup bucket:

```sh
garage bucket create <instance>-artifacts
garage key create <instance>-control          # read and write, the artifact writer
garage key create <instance>-workspace        # read-only, cold downloads
garage key create <instance>-checkpoint       # a separate prefix, the worker's checkpoint transport
garage bucket allow --read --write --owner <instance>-artifacts --key <instance>-control
garage bucket allow --read <instance>-artifacts --key <instance>-workspace
garage bucket allow --read --write <instance>-artifacts --key <instance>-checkpoint
```

Each key goes into its own standard-chain credential file (an `s3-credentials` and `s3-config` pair per service) in step 7. After the app Pod is running (step 8), configure the control database once, inside the control container, so the owner-only `control.sqlite3.artifacts/storage.toml` lands on the PVC ([S3 artifact backing](ARTIFACT-S3.md)):

```sh
kubectl -n research exec deploy/<name>-app -c control -- sandbox-science-artifacts configure \
  --database /var/lib/sandbox-science/control/control.sqlite3 \
  --bucket <instance>-artifacts --prefix artifacts \
  --endpoint https://<garage-s3-endpoint> --addressing-style path --region garage
```

Run it before any research data exists. Repeating the same configuration is safe; a different destination is refused.

Egress: add the Garage address to the bundle as an explicit entry for each Pod that reaches it (control and workspace are in the app Pod; the worker's checkpoint transport is in the pool Pod). Longhorn's own backup traffic leaves `longhorn-system`, outside this bundle:

```toml
[[network.app_egress]]
ports = [443]
peer.cidr = "<garage-ip>/32"

[[network.pool_egress]]
ports = [443]
peer.cidr = "<garage-ip>/32"
```

Verify: the SDK check `SS_INTEGRATION=1 SS_ARTIFACT_S3_TEST_BUCKET=<test bucket> SS_ARTIFACT_S3_TEST_ENDPOINT=https://<garage-s3-endpoint> SS_ARTIFACT_S3_TEST_REGION=garage uv run --extra s3 pytest -q packages/orchestrator/tests/test_artifact_s3.py::test_opt_in_compatible_endpoint` against a dedicated test bucket; then, after step 8, `sandbox-science-artifacts archive --database ...` on a first artifact. A bucket lifecycle rule must never expire referenced objects. The artifact bucket is part of the recovery set with the control PVC.

Rollback: the configuration is write-once; a different destination needs an explicit migration that keeps archived content. Do not delete the bucket once it holds a referenced artifact.

## 7. The bundle's Secrets

Create each Secret through the site's secure workflow, with exactly the keys the renderer copies ([KUBERNETES-BUNDLE](KUBERNETES-BUNDLE.md#prepare-existing-secrets-and-recovery-state)). Do not put a value in the bundle TOML, in shell arguments or in the rendered JSON.

| Secret (bundle setting) | Required keys | Notes |
| --- | --- | --- |
| `secrets.control` | `control.json`, `runtime-manager-token.sha256`, `worker-token`, `control.crt`, `control.key`, `worker-ca.crt` | `runtime-manager-token.sha256` is the lowercase SHA-256 hex of the manager's bearer token (the file `manager-control.token` in the manager's Secret). Add `extra_files` `s3-credentials`, `s3-config` for the writer key |
| `secrets.workspace` | `worker-token`, `worker-ca.crt` | `extra_files` `s3-credentials`, `s3-config` for the read-only key; `[workspace.environment]` names them |
| `secrets.worker` | `worker.toml`, `worker.crt`, `worker.key` | `extra_files` as needed: `pool-token`, `ceiling.yaml`, `targets.yaml`, the checkpoint S3 pair; the OpenShell CLI tree goes on the PVC |
| `secrets.middleware` | `middleware.toml`, `middleware.crt`, `middleware.key` | `extra_files` `ceiling.yaml`, `targets.yaml`, the gateway verifier's public key |
| `secrets.cache` (optional) | `cache.toml` | Needs `network.sandboxes` peers |
| `secrets.runtime_manager` | `runtime-manager.json`, `control-models.json`, `manager-control.token`, `model.key`, `control-ca.crt`, `active-gateway`, `gateway-metadata.json`, `gateway-ca.crt`, `gateway-tls.crt`, `gateway-tls.key` | The model key is the manager's, issued by the in-cluster model gateway for this instance; the profile files are from 5.1 |

Generate the tokens and certificates:

```sh
# tokens: one per link, 32 random bytes each; keep the manager token's digest separately for the control Secret
head -c 32 /dev/urandom | base64 -w0 > manager-control.token      # no trailing newline, so the digest below is of the token itself
sha256sum manager-control.token | cut -d' ' -f1 > runtime-manager-token.sha256
```

Certificates must cover `<name>-control.research.svc`, `<name>-worker.research.svc` and `<name>-middleware.research.svc`; issue them with cert-manager from a private issuer and put the CA in `worker-ca.crt` and `control-ca.crt`. Certificate and hostname verification stay on. Model aliases in `control.json` and `control-models.json` match `[models.*]` in the bundle TOML.

The pool's OpenShell CLI tree (the worker's profile from 5.1) is not projected from Secret keys. Place it on the pool PVC once the pool Pod runs, and set in the bundle `[worker.environment] HOME = "/var/lib/sandbox-science/worker-home"` and `XDG_CONFIG_HOME = "/var/lib/sandbox-science/worker-config"`:

```
worker-config/openshell/active_gateway
worker-config/openshell/gateways/<gateway>/metadata.json
worker-config/openshell/gateways/<gateway>/mtls/{ca.crt,tls.crt,tls.key}   # mode 600, owned by UID 1000
```

The worker's own settings (`worker.toml`) declare `isolation_tier = 3`, `node_kernel_releases = ["*-talos"]`, the kubernetes profiles of [worker-placement.toml](../examples/kubernetes/worker-placement.toml) (amd64 selector and `runtime_class_name = "kata"` on each), and the `minimal` and `crypto-base` digests. The middleware must be reachable and trusted by the gateway ([policy middleware](POLICY-MIDDLEWARE.md)). Put the worker, middleware and package-cache settings in the same combined unit as D101 lands.

Verify: `kubectl -n research get secret <each> -o json | jq '.data | keys'` matches the table; the renderer's `check` (step 8) finds every key.

Rollback: delete a Secret only while no Pod mounts it; a Pod re-reads credentials only at start.

## 8. Render, dry-run, apply

The private bundle TOML starts from [the template](../examples/kubernetes-bundle.toml): the six digests, `storage_class = "research-retain"` for app, pool and manager, `app.architecture` and `runtime_manager.architecture` for the arm64 nodes (the `trusted-app` image covers both), `pool.architecture = "amd64"`, the `network.*` peers (DNS labels of the cluster's CoreDNS, the OpenShell gateway at port 8080, the supervisors of `<gateway-id>` in `research-runtimes`), the egress entries of step 6 and the gateway, Garage and package-upstream destinations.

```sh
uv run sandbox-science-kubernetes check  --config /path/private-kubernetes.toml
uv run sandbox-science-kubernetes render --config /path/private-kubernetes.toml --output /path/new-bundle.json
kubectl diff -f /path/new-bundle.json
kubectl apply --dry-run=server -f /path/new-bundle.json
kubectl apply -f /path/new-bundle.json
kubectl -n research rollout status deploy/<name>-app deploy/<name>-pool deploy/<name>-runtime-manager
```

Inspect the JSON before applying: image digests, `kubernetes.io/arch` selectors, the storage class and size, the NetworkPolicies, and no Secret value. The renderer never overwrites an existing output file.

Verify: all three Deployments are `Available`; `kubectl -n research get pvc` shows three `Bound` claims on `research-retain` with the recurring jobs attached (Longhorn UI, volume details); the control Service accepts the first connection (`/ready`). Check the runtime manager: `kubectl -n research logs deploy/<name>-runtime-manager` reports waiting, not creating. Reach the GUI by port-forward to the exact application Pod, as [the bundle](KUBERNETES-BUNDLE.md#render-inspect-then-qualify-separately) says.

Rollback: set the three Deployments to zero replicas (`kubectl -n research scale deploy --all --replicas=0`). Never `kubectl delete -f` the whole bundle, and never delete the PVCs.

Now run the artifact `configure` command of step 6 and place the worker's CLI tree of step 7, then restart the pool Pod (`kubectl -n research rollout restart deploy/<name>-pool`).

## 9. Bootstrap the agent runtime

Only when control is ready and the manager is running, and the manager's PVC holds no `sandbox.json`:

```sh
kubectl -n research exec deploy/<name>-runtime-manager -- sandbox-science-runtime-manager request bootstrap
kubectl -n research exec deploy/<name>-runtime-manager -- sandbox-science-runtime-manager request status
```

Verify:
- `request status` shows a recorded `ss-orch-<10hex>` name and sandbox ID, and a connected runtime.
- `kubectl -n research-runtimes get pods -o custom-columns=NAME:.metadata.name,RUNTIMECLASS:.spec.runtimeClassName,NODE:.spec.nodeName` shows two Pods for the sandbox: the workload Pod with `kata` on an amd64 node, and the supervisor Pod under the default runtime. Its labels give the `openshell.ai/gateway-id` to check against `network.runtimes` (`kubectl -n research-runtimes get pods --show-labels`).
- Control's `/health` (through `kubectl -n research exec deploy/<name>-app -c workspace` or a port-forward) reports `runtime_ready`.

Never run `sandbox-science up` as a workaround, and never clear the manager's PVC to retry. A failed create with no ID and an absent name is cleared only by the operator's `request forget`. See [bootstrap](KUBERNETES-BUNDLE.md#bootstrap-the-agent-runtime).

Rollback: `request stop --delete` is the teardown; `request replace` replaces the sandbox explicitly. Deleting the manager Pod stops the runtime without deleting the sandbox.

## 10. The three live proofs

The deployment counts as deployed after all three ([D92](KUBERNETES.md#first-deployment-scope-and-proofs)). Make a throwaway project for the sandbox tests, with a policy that lists `github.com` for git, the package cache for pip and one reviewed MCP server, and no other host. Record each result.

Operator-side helper (a research sandbox's name is in the worker environment record or `openshell sandbox list`):

```sh
export OPENSHELL_EXEC='kubectl -n research exec deploy/<name>-pool -c worker -- env XDG_CONFIG_HOME=/var/lib/sandbox-science/worker-config openshell'
$OPENSHELL_EXEC sandbox list
$OPENSHELL_EXEC sandbox exec -n <sandbox> --no-tty --no-login-shell -- <command>
```

### 10.1 Egress only through OpenShell

In the throwaway project's shell (through the workspace GUI or the agent):

| Check | Command | Expected |
| --- | --- | --- |
| git | `git clone --depth 1 https://github.com/<owner>/<small-repo> /tmp/g && echo ok` | `ok` |
| pip through the cache | `pip download --no-deps -d /tmp/p <small-package>` | succeeds; the package cache's log shows the request |
| MCP | call the reviewed server's read tool from the session | answer returned; an MCP event in the event store |
| unlisted host | `curl -sS -m 10 https://<unlisted-host>/` | fails with a policy denial; the denial is in the middleware events and the supervisor's logs (`kubectl -n research-runtimes logs -l openshell.ai/component=supervisor`) |
| raw socket to an IP | `python3 -c "import socket; socket.create_connection(('<public-ip>', 443), 5)"` | exception: timeout or unreachable |
| LAN | the same with a LAN address, port 22 or 443 | exception |
| other pods | the same with a pod IP from `kubectl get pods -A -o wide` (the app Pod, the control Service on 8443, the gateway on 8080, the Kubernetes API Service on 443) | exception |

Also confirm NetworkPolicy enforcement independently of OpenShell: from a plain pod in `research-runtimes` carrying the sandbox workload labels, a new outbound connection must fail. Rendering and the CNI's behavior are separate facts ([enforcement](KUBERNETES.md#sandbox-isolation)).

### 10.2 Kata in effect and tier 3 recorded

```sh
kubectl -n research-runtimes get pods -o custom-columns=NAME:.metadata.name,RUNTIMECLASS:.spec.runtimeClassName,NODE:.spec.nodeName
$OPENSHELL_EXEC sandbox exec -n <sandbox> --no-tty --no-login-shell -- uname -r      # not *-talos
kubectl -n research exec deploy/<name>-app -c workspace -- python3 - <<'EOF'
import json, ssl, urllib.request
ctx = ssl.create_default_context(cafile="/private/worker-ca.crt")
req = urllib.request.Request(
    "https://<name>-worker.research.svc:8100/v1/capabilities?environment_id=<environment>",
    headers={"Authorization": "Bearer " + open("/private/worker-token").read().strip()})
print(json.load(urllib.request.urlopen(req, context=ctx))["location"])
EOF
```

Expected: the workload Pod's `RUNTIMECLASS` is `kata` on an amd64 node; the guest kernel is not `-talos`; the location shows `isolation_tier` 3 with the declared tier 3.

The runc case, once: add a test-only RuntimeClass with handler `runc` (the label `openshell.ai/sandbox-attachable: "true"`), a temporary worker profile that names it, create an environment on it, and read its location: the guest reports a `-talos` kernel, matches `node_kernel_releases`, and is recorded at tier 1. Remove the profile and the RuntimeClass afterwards.

### 10.3 Manager cycle

```sh
M="kubectl -n research exec deploy/<name>-runtime-manager -- sandbox-science-runtime-manager request"
$M status                                            # note the sandbox name and ID
kubectl -n research delete pod -l app.kubernetes.io/part-of=<name>,app.kubernetes.io/component=app
kubectl -n research delete pod -l app.kubernetes.io/part-of=<name>,app.kubernetes.io/component=runtime-manager
kubectl -n research rollout status deploy/<name>-app deploy/<name>-runtime-manager
$M status                                            # same sandbox ID, runtime connected again
$M replace && $M status                              # a new name and ID
```

Expected: after the kill, the manager reconnects to the same sandbox ID without creating one; control revokes the old token and issues a new one; after `replace`, the old sandbox is gone and a new one is recorded; the projects, sessions and events from before are present in the GUI, and the event store's chain verifies:

```sh
kubectl -n research exec deploy/<name>-app -c control -- python3 -c \
 "import sqlite3; print(sqlite3.connect('file:/var/lib/sandbox-science/control/control.sqlite3?mode=ro', uri=True).execute('pragma integrity_check').fetchone())"
```

Orphan survival after an abrupt gateway loss (restart the gateway Pod during a run) is a separate case: confirm that the next start revokes first and no second runtime connects ([fencing](specs/kubernetes-runtime-bootstrap.md#process-model-exec-session-not-main-process)).

## 11. Log collection

D103: structured JSON logs from every service, OpenShell's OCSF events and the proxy and middleware records go to Loki through Alloy. The bundle already makes each container log JSON lines and labels each Pod ([bundle: log collection](KUBERNETES-BUNDLE.md#log-collection)); this step installs the collector. Do it before the live proofs of step 10, so their denials are kept.

1. **Install Loki** (3.x, for structured metadata) with storage and retention you choose; the proofs' evidence needs at least the length of the qualification (a month is a start), and the bundle sets none. Note the push URL.
2. **Install Alloy** where it can read the namespace's Pod logs: `get`, `list` and `watch` on `pods` and `get` on `pods/log` for its ServiceAccount, in the bundle's namespace and the sandboxes' namespace. It needs no NetworkPolicy ingress rule on the bundle's Pods; it needs egress to Loki.
3. **Configure it from the sample.** Copy [alloy-config.alloy](../examples/kubernetes/alloy-config.alloy) outside Git; set the namespaces (the sample has `research` and `research-runtimes`) and the Loki URL.
4. **OpenShell's events.** Turn the supervisors' OCSF JSON on (`openshell settings set --global --key ocsf_json_enabled --value true`) and, if you export over OTLP, add `[openshell.gateway.otlp]` and `[openshell.gateway.otlp.ocsf]` to the gateway's configuration with the Alloy listener's address (the sample's OTLP receiver) as the endpoint; the listener must be reachable from the sandboxes, which the sandbox network policy and a NetworkPolicy must allow. The gateway's metrics listener (`metrics_bind_address`) is for Prometheus, if you run one.
5. **Check it.** `kubectl -n research logs deploy/<name>-pool -c worker --tail 3` shows JSON lines; in Loki, `{service="worker"} | json` returns the same lines, and `{service="middleware"} | json | event != ""` finds the middleware's records. Trigger the denial of 10.1 and find it by its OCSF line. A container that logs text (no `service` field) is a bundle rendered before this change.
6. **Keep the proofs' results** (steps 10.1 to 10.3) with the deployment record, and the dashboards and alerts (D103's later steps) wait until the three proofs have passed.

## Order of the rollback points

A snapshot before each of: step 8's first apply, the bootstrap, and every image or schema change. Before restoring, stop the Pods that use a volume. The coordinated recovery set is the control PVC, the pool PVC, the manager PVC, the Garage artifact bucket and the Longhorn backups together; one alone does not authorize deleting a research PVC ([storage](KUBERNETES.md#storage-lifecycle)).
