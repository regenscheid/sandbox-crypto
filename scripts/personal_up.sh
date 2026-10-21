#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Start the owner's personal Sandbox Science on babbage (docs/PERSONAL-BABBAGE.md): its own
# OpenShell gateway (VM driver), policy middleware and package cache, then the launcher,
# each a transient `systemd-run --user` unit named "$UNIT-*". Everything it makes is under
# $ROOT and survives a stop: the first run creates it, later runs reuse it.
#
#   scripts/personal_up.sh [--openshell-only]
#
# Settings, from the environment (the defaults are babbage's personal instance):
#   SS_PERSONAL_ROOT     ~/.local/state/sandbox-science-personal  (OpenShell state in openshell/)
#   SS_PERSONAL_CONFIG   ~/.config/sandbox-science/personal.toml  (the launcher's config)
#   SS_PERSONAL_UNIT     ss-personal                              (the units' prefix)
#   SS_PERSONAL_GATEWAY_PORT 17680, SS_PERSONAL_MIDDLEWARE_PORT 50061,
#   SS_PERSONAL_CACHE_PORT 8390, SS_PERSONAL_PROXY_PORT 8380      (loopback ports)
#   VM_VCPUS 8, VM_MEM_MIB 16384, VM_DISK_MIB 32768              (each sandbox's VM)
#   OS_BIN ~/.local/opt/openshell-0.1.5/bin, R the repository (this script's by default)
#
# None of it is the labs' (.agents/labs/lab_up.sh: ~/.local/state/openshell-lab, units
# openshell-lab-*, ports 17670, 50051, 8090, 8100, 8200), and lab_down.sh leaves it alone.
set -euo pipefail
R=${R:-$(cd "$(dirname "$0")/.." && pwd)}
ROOT=${SS_PERSONAL_ROOT:-$HOME/.local/state/sandbox-science-personal}
CONFIG=${SS_PERSONAL_CONFIG:-$HOME/.config/sandbox-science/personal.toml}
UNIT=${SS_PERSONAL_UNIT:-ss-personal}
GATEWAY_PORT=${SS_PERSONAL_GATEWAY_PORT:-17680}
MIDDLEWARE_PORT=${SS_PERSONAL_MIDDLEWARE_PORT:-50061}
CACHE_PORT=${SS_PERSONAL_CACHE_PORT:-8390}
PROXY_PORT=${SS_PERSONAL_PROXY_PORT:-8380}
OS_BIN=${OS_BIN:-$HOME/.local/opt/openshell-0.1.5/bin}
OPENSHELL_ONLY=false
case "${1:-}" in
  "") ;;
  --openshell-only) OPENSHELL_ONLY=true ;;
  *) echo "usage: $0 [--openshell-only]" >&2; exit 2 ;;
esac
case "$UNIT" in openshell-lab*) echo "the units' prefix must not be the labs'" >&2; exit 2 ;; esac
case "$ROOT" in "$HOME/.local/state/openshell"*) echo "the root must not be under the labs' paths" >&2; exit 2 ;; esac

OS="$ROOT/openshell"
active() { systemctl --user is-active -q "$1" 2>/dev/null; }
listening() { ss -ltn | grep -qE "127\.0\.0\.1:$1\b"; }

if active "$UNIT-launcher"; then echo "$UNIT-launcher is already running" >&2; exit 1; fi
if ! $OPENSHELL_ONLY && [ ! -f "$CONFIG" ]; then echo "no config at $CONFIG" >&2; exit 1; fi
running=0
for part in cache middleware gateway; do active "$UNIT-$part" && running=$((running + 1)); done
if [ "$running" = 3 ]; then
  echo "OpenShell: $UNIT-cache, $UNIT-middleware and $UNIT-gateway already run"
  START_OPENSHELL=false
elif [ "$running" != 0 ]; then
  echo "only some of $UNIT-cache, $UNIT-middleware and $UNIT-gateway run: scripts/personal_down.sh first" >&2; exit 1
else
  START_OPENSHELL=true
  for port in "$GATEWAY_PORT" "$MIDDLEWARE_PORT" "$CACHE_PORT"; do
    if listening "$port"; then echo "port $port is busy" >&2; exit 1; fi
  done
fi

start_openshell() {
# The environment every OpenShell command of this instance needs; `source` it to use the CLI.
mkdir -p "$ROOT" "$OS" && chmod 700 "$ROOT" "$OS"
cat > "$OS/env.sh" <<EOF
export OS_BIN="$OS_BIN"
export XDG_CONFIG_HOME="$OS/config"
export XDG_STATE_HOME="$OS/state"
export XDG_DATA_HOME="$OS/data"
export OPENSHELL_LOCAL_TLS_DIR="$OS/state/openshell/tls"
export OPENSHELL_TELEMETRY_ENABLED=false
export PATH="\$OS_BIN:\$PATH"
EOF
# shellcheck source=/dev/null
source "$OS/env.sh"
L="$XDG_STATE_HOME/openshell"
MW="$OS/middleware/tls"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_STATE_HOME" "$XDG_DATA_HOME" "$MW" "$OS/middleware/state" "$OS/cache"

# Made once and kept: the gateway's certificates and JWT keys (its sandboxes trust them),
# the middleware's own CA and certificate, and the policy store.
if [ ! -f "$OPENSHELL_LOCAL_TLS_DIR/ca.crt" ]; then
  "$OS_BIN/openshell-gateway" generate-certs --output-dir "$OPENSHELL_LOCAL_TLS_DIR" --server-san host.openshell.internal >/dev/null 2>&1
fi
if [ ! -f "$MW/middleware.pem" ]; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 -subj "/CN=sandbox-science personal middleware CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign" -keyout "$MW/ca-key.pem" -out "$MW/ca.pem" 2>/dev/null
  openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=sandbox-science-policy" -keyout "$MW/middleware-key.pem" -out "$MW/middleware.csr" 2>/dev/null
  printf '%s\n' 'basicConstraints=critical,CA:FALSE' 'keyUsage=critical,digitalSignature' 'extendedKeyUsage=serverAuth' 'subjectAltName=IP:127.0.0.1,DNS:localhost' > "$MW/server-ext.cnf"
  openssl x509 -req -in "$MW/middleware.csr" -CA "$MW/ca.pem" -CAkey "$MW/ca-key.pem" -CAcreateserial -days 3650 -sha256 -extfile "$MW/server-ext.cnf" -out "$MW/middleware.pem" 2>/dev/null
  chmod 600 "$MW/ca-key.pem" "$MW/middleware-key.pem"
fi
if [ ! -f "$OS/middleware/pool-ceiling.yaml" ]; then
  printf '%s\n' 'apiVersion: sandbox-science/v0alpha1' 'kind: PoolCeiling' > "$OS/middleware/pool-ceiling.yaml"
  "$R/.venv/bin/sandbox-science-policy-middleware" store --state-dir "$OS/middleware/state" --policy "$R/examples/policy.yaml" >/dev/null
fi

# Written at every start, from the settings above.
cat > "$OS/middleware/middleware.toml" <<EOF
registration = "sandbox-science-policy"
listen = "127.0.0.1:$MIDDLEWARE_PORT"
state_dir = "state"
ceiling = "pool-ceiling.yaml"

[tls]
certificate = "tls/middleware.pem"
private_key = "tls/middleware-key.pem"

[tokens]
gateway_id = "openshell"
public_key = "$OPENSHELL_LOCAL_TLS_DIR/jwt/public.pem"
EOF
printf '%s\n' "listen = \"127.0.0.1:$CACHE_PORT\"" "public_url = \"http://host.openshell.internal:$CACHE_PORT\"" 'cache_dir = "cache"' 'max_size = "20Gi"' '' '[pypi]' '[conda]' '[cargo]' '[go]' '[npm]' > "$OS/cache.toml"
cat > "$OS/targets.yaml" <<EOF
apiVersion: sandbox-science/v0alpha1
kind: OpenShellTargets
sandbox:
  middleware: sandbox-science-policy
  package_cache: {host: host.openshell.internal, port: $CACHE_PORT}
  mcp_proxy: {host: host.openshell.internal, port: $PROXY_PORT}
  binaries: [/**]
  filesystem: {read_only: [/var/lib/texmf], read_write: [/opt/sandbox-science]}
mcp_proxy:
  binaries: [/opt/sandbox-science/bin/python*]
  versions: ["2025-11-25"]
  filesystem: {read_only: [/opt/sandbox-science]}
EOF
cat > "$OS/gateway.toml" <<EOF
[openshell]
version = 2

[openshell.gateway]
bind_address   = "127.0.0.1:$GATEWAY_PORT"
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

# The VM driver's state is kept short, at $ROOT/vm: its sockets' paths must fit SUN_LEN.
[openshell.drivers.vm]
driver_dir       = "$OS_BIN"
state_dir        = "$ROOT/vm"
grpc_endpoint    = "https://127.0.0.1:$GATEWAY_PORT"
default_image    = "nvcr.io/nvidia/base/ubuntu:24.04"
bootstrap_image  = "nvcr.io/nvidia/base/ubuntu:24.04"
vcpus            = ${VM_VCPUS:-8}
mem_mib          = ${VM_MEM_MIB:-16384}
overlay_disk_mib = ${VM_DISK_MIB:-32768}

[[openshell.supervisor.middleware]]
name              = "sandbox-science-policy"
grpc_endpoint     = "https://127.0.0.1:$MIDDLEWARE_PORT"
tls_ca_cert_path  = "$MW/ca.pem"
max_payload_bytes = 4194304
timeout           = "500ms"
EOF
chmod 600 "$OS/gateway.toml"

systemd-run --user --unit="$UNIT-cache" --collect -q -p LimitNOFILE=65536 --working-directory="$OS" \
  "$R/.venv/bin/sandbox-science-package-cache" serve --settings "$OS/cache.toml" --log-level info
systemd-run --user --unit="$UNIT-middleware" --collect -q --working-directory="$OS/middleware" \
  "$R/.venv/bin/sandbox-science-policy-middleware" serve --settings "$OS/middleware/middleware.toml" --log-level INFO
for _ in $(seq 1 40); do listening "$MIDDLEWARE_PORT" && listening "$CACHE_PORT" && break; sleep 0.5; done
systemd-run --user --unit="$UNIT-gateway" --collect -q -p KillMode=mixed -p TimeoutStopSec=90 --working-directory="$OS" \
  --setenv=XDG_CONFIG_HOME="$XDG_CONFIG_HOME" --setenv=XDG_STATE_HOME="$XDG_STATE_HOME" --setenv=XDG_DATA_HOME="$XDG_DATA_HOME" \
  --setenv=OPENSHELL_LOCAL_TLS_DIR="$OPENSHELL_LOCAL_TLS_DIR" --setenv=OPENSHELL_TELEMETRY_ENABLED=false \
  "$OS_BIN/openshell-gateway" --config "$OS/gateway.toml"
for _ in $(seq 1 40); do listening "$GATEWAY_PORT" && break; sleep 0.5; done
if ! "$OS_BIN/openshell" gateway list 2>/dev/null | grep -q "127.0.0.1:$GATEWAY_PORT"; then
  "$OS_BIN/openshell" gateway add "https://127.0.0.1:$GATEWAY_PORT" --local --name openshell >/dev/null 2>&1
fi
for part in cache middleware gateway; do
  active "$UNIT-$part" || { echo "$UNIT-$part did not start: journalctl --user -u $UNIT-$part" >&2; exit 1; }
done
# The example policy binds the provider github-readonly, which OpenShell must have before
# it creates a research sandbox. As in the labs, it gets a placeholder that GitHub refuses:
# public repositories work without it. A real fine-grained read-only token replaces it with
# `openshell provider update` (docs/PERSONAL-BABBAGE.md).
if ! "$OS_BIN/openshell" provider list 2>/dev/null | grep -q 'github-readonly'; then
  "$OS_BIN/openshell" provider profile import -f "$R/examples/openshell/profiles/github-token.yaml" >/dev/null 2>&1 || true
  GITHUB_TOKEN="placeholder-not-a-token-$(openssl rand -hex 8)" \
    "$OS_BIN/openshell" provider create --name github-readonly --type github-token --credential GITHUB_TOKEN >/dev/null
fi
echo "OpenShell: $UNIT-cache, $UNIT-middleware and $UNIT-gateway are running (state in $OS)"
}
$START_OPENSHELL && start_openshell
$OPENSHELL_ONLY && exit 0

# The launcher, which stops its parts in order on SIGTERM: KillMode=mixed sends it alone the
# signal, and the timeout covers the research sandboxes' suspension.
systemd-run --user --unit="$UNIT-launcher" --collect -q -p KillMode=mixed -p TimeoutStopSec=300 --working-directory="$R" \
  "$R/.venv/bin/sandbox-science" up --config "$CONFIG"
echo "launcher: $UNIT-launcher started; waiting for it to report it runs (the first start imports skills and boots the MCP proxy's sandbox)"
INVOCATION=$(systemctl --user show -p InvocationID --value "$UNIT-launcher")
for _ in $(seq 1 180); do
  if ! active "$UNIT-launcher"; then
    echo "the launcher stopped: journalctl --user -u $UNIT-launcher" >&2; exit 1
  fi
  if journalctl --user _SYSTEMD_INVOCATION_ID="$INVOCATION" -o cat --no-pager 2>/dev/null | grep -q 'Sandbox Science is running'; then
    "$R/.venv/bin/sandbox-science" status --config "$CONFIG" && exit 0
  fi
  sleep 5
done
echo "the launcher has not reported it runs after 15 minutes: journalctl --user -u $UNIT-launcher" >&2
exit 1
