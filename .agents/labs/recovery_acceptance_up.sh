#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Disposable labs only (recovery_acceptance.py, direct_mcp.py), which generate the required
# environment. MIDDLEWARE_EXTRA, if set, adds lines to the middleware's settings.
set -euo pipefail

: "${R:?}" "${LAB:?}" "${LAB_UNIT:?}" "${OS_BIN:?}"
test "${LAB%/*}" = /var/tmp && [[ "${LAB##*/}" = ss-recovery-* || "${LAB##*/}" = ss-direct-mcp-* ]]
test -f "$LAB/identity.json" && test -f "$LAB/env.sh"
test ! -e "$LAB/gateway.toml" || { echo 'already prepared' >&2; exit 1; }
# shellcheck source=/dev/null
source "$LAB/env.sh"
L="$XDG_STATE_HOME/openshell"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_STATE_HOME" "$XDG_DATA_HOME" "$LAB/policies" "$LAB/middleware/tls" "$LAB/middleware/state" "$LAB/cache"
"$OS_BIN/openshell-gateway" generate-certs --output-dir "$OPENSHELL_LOCAL_TLS_DIR" --server-san host.openshell.internal >/dev/null 2>&1
MW="$LAB/middleware/tls"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 30 -subj "/CN=babbage-lab middleware CA" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign" -keyout "$MW/ca-key.pem" -out "$MW/ca.pem" 2>/dev/null
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=sandbox-science-policy" -keyout "$MW/middleware-key.pem" -out "$MW/middleware.csr" 2>/dev/null
printf '%s\n' 'basicConstraints=critical,CA:FALSE' 'keyUsage=critical,digitalSignature' 'extendedKeyUsage=serverAuth' 'subjectAltName=IP:127.0.0.1,DNS:localhost' > "$MW/server-ext.cnf"
openssl x509 -req -in "$MW/middleware.csr" -CA "$MW/ca.pem" -CAkey "$MW/ca-key.pem" -CAcreateserial -days 30 -sha256 -extfile "$MW/server-ext.cnf" -out "$MW/middleware.pem" 2>/dev/null
chmod 600 "$MW/ca-key.pem" "$MW/middleware-key.pem"
printf '%s\n' 'apiVersion: sandbox-science/v0alpha1' 'kind: PoolCeiling' > "$LAB/middleware/pool-ceiling.yaml"
cat > "$LAB/middleware/middleware.toml" <<EOF
registration = "sandbox-science-policy"
listen = "127.0.0.1:${PORT_MIDDLEWARE}"
state_dir = "state"
ceiling = "pool-ceiling.yaml"
${MIDDLEWARE_EXTRA:-}

[tls]
certificate = "tls/middleware.pem"
private_key = "tls/middleware-key.pem"

[tokens]
gateway_id = "openshell"
public_key = "$OPENSHELL_LOCAL_TLS_DIR/jwt/public.pem"
EOF
printf '%s\n' "listen = \"127.0.0.1:${PORT_CACHE}\"" "public_url = \"http://host.openshell.internal:${PORT_CACHE}\"" 'cache_dir = "cache"' 'max_size = "20Gi"' '' '[pypi]' '[conda]' '[cargo]' '[go]' '[npm]' > "$LAB/cache.toml"
cat > "$LAB/targets.yaml" <<EOF
apiVersion: sandbox-science/v0alpha1
kind: OpenShellTargets
sandbox:
  middleware: sandbox-science-policy
  package_cache: {host: host.openshell.internal, port: ${PORT_CACHE}}
  mcp_proxy: {host: host.openshell.internal, port: ${PORT_UNUSED}}
  binaries: [/**]
  filesystem: {read_only: [/var/lib/texmf], read_write: [/opt/sandbox-science]}
mcp_proxy:
  binaries: [/opt/sandbox-science/bin/python*]
  versions: ["2025-11-25"]
  filesystem: {read_only: [/opt/sandbox-science]}
EOF
cat > "$LAB/gateway.toml" <<EOF
[openshell]
version = 2

[openshell.gateway]
bind_address   = "127.0.0.1:${PORT_GATEWAY}"
log_level      = "info"
compute_driver = "vm"
guest_tls_ca   = "$L/tls/ca.crt"

[openshell.gateway.tls]
cert_path      = "$L/tls/server/tls.crt"
key_path       = "$L/tls/server/tls.key"
client_ca_path = "$L/tls/ca.crt"

[openshell.gateway.mtls_auth]
enabled = true

[openshell.gateway.gateway_jwt]
signing_key_path = "$L/tls/jwt/signing.pem"
public_key_path  = "$L/tls/jwt/public.pem"
kid_path         = "$L/tls/jwt/kid"
gateway_id       = "openshell"

[openshell.drivers.vm]
driver_dir       = "$OS_BIN"
state_dir        = "$L/vm-driver"
grpc_endpoint    = "https://127.0.0.1:${PORT_GATEWAY}"
default_image    = "nvcr.io/nvidia/base/ubuntu:24.04"
bootstrap_image  = "nvcr.io/nvidia/base/ubuntu:24.04"
vcpus            = ${VM_VCPUS:-1}
mem_mib          = ${VM_MEM_MIB:-8192}
overlay_disk_mib = ${VM_DISK_MIB:-16384}

[[openshell.supervisor.middleware]]
name              = "sandbox-science-policy"
grpc_endpoint     = "https://127.0.0.1:${PORT_MIDDLEWARE}"
tls_ca_cert_path  = "$LAB/middleware/tls/ca.pem"
max_payload_bytes = 4194304
timeout           = "500ms"
EOF
chmod 600 "$LAB/gateway.toml"
"$R/.venv/bin/python" -I -B - "$R/examples/policy.yaml" "$LAB/targets.yaml" "$LAB/policies/research-lab.yaml" <<'PY'
import sys
from sandbox_science.policy import load_policy, PoolCeiling
from sandbox_science.policy.openshell import compile_sandbox, load_targets
policy = load_policy(sys.argv[1]); targets = load_targets(sys.argv[2])
open(sys.argv[3], "w").write(compile_sandbox(policy, targets.sandbox, ceiling=PoolCeiling.unrestricted()).policy.to_yaml())
PY
"$R/.venv/bin/sandbox-science-policy-middleware" store --state-dir "$LAB/middleware/state" --policy "$R/examples/policy.yaml" >/dev/null
systemd-run --user --unit=${LAB_UNIT}-cache --collect -q -p LimitNOFILE=65536 --working-directory="$LAB" "$R/.venv/bin/sandbox-science-package-cache" serve --settings "$LAB/cache.toml" --log-level info
systemd-run --user --unit=${LAB_UNIT}-middleware --collect -q --working-directory="$LAB/middleware" "$R/.venv/bin/sandbox-science-policy-middleware" serve --settings "$LAB/middleware/middleware.toml" --log-level INFO
for _ in $(seq 1 40); do ss -ltn | grep -q "127.0.0.1:${PORT_MIDDLEWARE}" && ss -ltn | grep -q "127.0.0.1:${PORT_CACHE}" && break; sleep 0.5; done
systemd-run --user --unit=${LAB_UNIT}-gateway --collect -q -p KillMode=mixed -p TimeoutStopSec=90 --working-directory="$LAB" "${OS_RUN_ENV[@]}" \
  "$OS_BIN/openshell-gateway" --config "$LAB/gateway.toml"
for _ in $(seq 1 40); do ss -ltn | grep -q "127.0.0.1:${PORT_GATEWAY}" && break; sleep 0.5; done
openshell gateway add https://127.0.0.1:${PORT_GATEWAY} --local --name openshell >/dev/null 2>&1
systemctl --user is-active ${LAB_UNIT}-cache ${LAB_UNIT}-middleware ${LAB_UNIT}-gateway | tr '\n' ' '; echo
