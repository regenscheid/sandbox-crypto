#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Start the owner's personal Sandbox Science on babbage (docs/PERSONAL-BABBAGE.md): the
# worker-side unit (the worker, the policy middleware and the package cache as one
# supervised group, docs/UNIT.md), its own OpenShell gateway (VM driver), then the
# launcher, each a transient `systemd-run --user` unit named "$UNIT-unit", "$UNIT-gateway"
# and "$UNIT-launcher". The unit starts first: the gateway checks the middleware when it
# starts, and the launcher uses the unit's worker. Everything it makes is under $ROOT and
# survives a stop: the first run creates it, later runs reuse it.
#
#   scripts/personal_up.sh [--openshell-only] [--skip-snapshot]
#
# Before it starts anything it asks `personal_backup.sh --versions` whether this checkout would
# migrate a store (migrations only go forward, docs/BACKUP-RESTORE.md). If so it takes a
# snapshot first (`personal_backup.sh --label before-migration`) and starts nothing if that
# fails. --skip-snapshot, or SS_PERSONAL_SKIP_SNAPSHOT=1, skips both the check and the snapshot.
#
# Settings, from the environment (the defaults are babbage's personal instance):
#   SS_PERSONAL_ROOT     ~/.local/state/sandbox-science-personal  (OpenShell state in openshell/)
#   SS_PERSONAL_CONFIG   ~/.config/sandbox-science/personal.toml  (the launcher's config)
#   SS_PERSONAL_UNIT     ss-personal                              (the units' prefix)
#   SS_PERSONAL_SKIP_SNAPSHOT  unset; 1 skips the pre-migration snapshot (as --skip-snapshot)
#   SS_PERSONAL_GATEWAY_PORT 17680, SS_PERSONAL_MIDDLEWARE_PORT 50061,
#   SS_PERSONAL_CACHE_PORT 8390                                   (loopback ports)
#   VM_VCPUS 8, VM_MEM_MIB 16384, VM_DISK_MIB 32768              (each sandbox's VM)
#   OS_BIN ~/.local/opt/openshell-<version>/bin, where <version> is OPENSHELL_VERSION in
#   packages/contracts/.../openshell_version.py (the script refuses any other release);
#   R the repository (this script's by default)
#
# The config must name the unit: personal.toml's [worker] has `unit = "$ROOT/openshell/unit.toml"`.
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
# The one OpenShell release the code supports, read from the constant that names it
# (contracts/openshell_version.py), from the checkout this script is in.
VERSION_FILE="$(cd "$(dirname "$0")/.." && pwd)/packages/contracts/src/sandbox_science/contracts/openshell_version.py"
OS_VERSION=$(sed -n 's/^OPENSHELL_VERSION: Final = "\([0-9][0-9.]*\)"$/\1/p' "$VERSION_FILE" 2>/dev/null)
if [ -z "$OS_VERSION" ]; then echo "no OPENSHELL_VERSION in $VERSION_FILE" >&2; exit 1; fi
OS_BIN=${OS_BIN:-$HOME/.local/opt/openshell-$OS_VERSION/bin}
OPENSHELL_ONLY=false
SKIP_SNAPSHOT=false
[ "${SS_PERSONAL_SKIP_SNAPSHOT:-}" = 1 ] && SKIP_SNAPSHOT=true
for arg in "$@"; do
  case "$arg" in
    --openshell-only) OPENSHELL_ONLY=true ;;
    --skip-snapshot) SKIP_SNAPSHOT=true ;;
    *) echo "usage: $0 [--openshell-only] [--skip-snapshot]" >&2; exit 2 ;;
  esac
done
case "$UNIT" in openshell-lab*) echo "the units' prefix must not be the labs'" >&2; exit 2 ;; esac
case "$ROOT" in "$HOME/.local/state/openshell"*) echo "the root must not be under the labs' paths" >&2; exit 2 ;; esac

OS="$ROOT/openshell"
UNIT_CONFIG="$OS/unit.toml"
active() { systemctl --user is-active -q "$1" 2>/dev/null; }
listening() { ss -ltn | grep -qE "127\.0\.0\.1:$1\b"; }

if active "$UNIT-launcher"; then echo "$UNIT-launcher is already running" >&2; exit 1; fi
# Nothing starts on another release of OpenShell: its gateway, CLI and supervisor are
# versioned together, and the code follows one release's behavior.
OS_SAID=$("$OS_BIN/openshell" --version 2>&1) || true
case " $OS_SAID " in
  *" $OS_VERSION "*|*" v$OS_VERSION "*) ;;
  *) echo "$OS_BIN/openshell says '${OS_SAID:-nothing}', but this checkout needs OpenShell $OS_VERSION (OS_BIN names the directory of that release)" >&2; exit 1 ;;
esac

# The unit's worker takes its settings, with the clients' tokens, from the launcher's config,
# so even --openshell-only needs it.
if [ ! -f "$CONFIG" ]; then echo "no config at $CONFIG" >&2; exit 1; fi
for part in cache middleware; do
  if active "$UNIT-$part"; then
    echo "$UNIT-$part, a unit of the earlier layout, runs: scripts/personal_down.sh first" >&2; exit 1
  fi
done
running=0
for part in unit gateway; do active "$UNIT-$part" && running=$((running + 1)); done
if [ "$running" = 2 ]; then
  echo "OpenShell: $UNIT-unit and $UNIT-gateway already run"
  START_OPENSHELL=false
elif [ "$running" != 0 ]; then
  echo "only one of $UNIT-unit and $UNIT-gateway runs: scripts/personal_down.sh first" >&2; exit 1
else
  START_OPENSHELL=true
  for port in "$GATEWAY_PORT" "$MIDDLEWARE_PORT" "$CACHE_PORT"; do
    if listening "$port"; then echo "port $port is busy" >&2; exit 1; fi
  done
fi

# The tokens and the worker's settings, written by the launcher's own code and nothing
# started. The unit runs that settings file, so the config must name this unit.
PREPARED=$("$R/.venv/bin/sandbox-science" prepare --config "$CONFIG") || exit 1
WORKER_SETTINGS=$(printf '%s\n' "$PREPARED" | sed -n 's/^worker_settings=//p')
CONFIG_UNIT=$(printf '%s\n' "$PREPARED" | sed -n 's/^unit=//p')
if [ -z "$WORKER_SETTINGS" ] || [ "$CONFIG_UNIT" != "$UNIT_CONFIG" ]; then
  echo "$CONFIG must name the unit: under [worker], unit = \"$UNIT_CONFIG\" (it says: ${CONFIG_UNIT:-nothing})" >&2
  exit 1
fi
WORKER_DIGEST=$(sha256sum "$WORKER_SETTINGS" | cut -d' ' -f1)
if ! $START_OPENSHELL && [ "$(cat "$OS/unit-worker.sha256" 2>/dev/null)" != "$WORKER_DIGEST" ]; then
  echo "the running unit has older worker settings than $CONFIG now gives: scripts/personal_down.sh, then this" >&2
  exit 1
fi

# A start migrates a store that is older than this checkout, and a migration cannot be
# undone. So when `--versions` (exit 3) says one would, the snapshot comes first, before
# anything starts; if it fails, nothing starts. A store newer than the checkout, or a check
# that cannot tell, would make the start refuse anyway, so it stops here with the reason.
SELF=$(cd "$(dirname "$0")" && pwd)
snapshot_before_migration() {
  local status=0
  R="$R" "$SELF/personal_backup.sh" --versions || status=$?
  case "$status" in
    0) return 0 ;;
    3)
      echo "a start would migrate a store: taking a snapshot first (label before-migration)" >&2
      if ! R="$R" "$SELF/personal_backup.sh" --label before-migration; then
        echo "the snapshot failed, so nothing was started (fix it, or --skip-snapshot to start without one)" >&2
        return 1
      fi
      ;;
    *)
      echo "the stores cannot be started by this checkout (see above), so nothing was started (--skip-snapshot skips this check)" >&2
      return 1
      ;;
  esac
}
if $SKIP_SNAPSHOT; then
  echo "snapshot: skipped (--skip-snapshot or SS_PERSONAL_SKIP_SNAPSHOT)" >&2
else
  snapshot_before_migration || exit 1
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
mkdir -p "$XDG_CONFIG_HOME" "$XDG_STATE_HOME" "$XDG_DATA_HOME" "$MW" "$OS/middleware/state" "$OS/cache" "$OS/unit"

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

# The unit's one config, written at every start from the settings above: the worker's settings
# are the launcher's (`prepare` wrote them), the middleware's and the cache's are here.
cat > "$UNIT_CONFIG" <<EOF
[unit]
bin_dir = "$R/.venv/bin"
runtime_dir = "$OS/unit"
stop_timeout = 30

[worker]
settings_file = "$WORKER_SETTINGS"

[middleware]
listen = "127.0.0.1:$MIDDLEWARE_PORT"
registration = "sandbox-science-policy"
state_dir = "$OS/middleware/state"
ceiling = "$OS/middleware/pool-ceiling.yaml"
# The agent runtime's MCP listings (D98: the direct path, the only one since the MCP proxy
# retired), which the middleware reads to drop denied tools; [mcp_direct] needs it.
mcp_listings = true

[middleware.tls]
certificate = "$MW/middleware.pem"
private_key = "$MW/middleware-key.pem"

[middleware.tokens]
gateway_id = "openshell"
public_key = "$OPENSHELL_LOCAL_TLS_DIR/jwt/public.pem"

[cache]
listen = "127.0.0.1:$CACHE_PORT"
public_url = "http://host.openshell.internal:$CACHE_PORT"
cache_dir = "$OS/cache"
max_size = "20Gi"

[cache.pypi]
[cache.conda]
[cache.cargo]
[cache.go]
[cache.npm]
EOF
chmod 600 "$UNIT_CONFIG"
cat > "$OS/targets.yaml" <<EOF
apiVersion: sandbox-science/v0alpha1
kind: OpenShellTargets
sandbox:
  middleware: sandbox-science-policy
  package_cache: {host: host.openshell.internal, port: $CACHE_PORT}
  binaries: [/**]
  filesystem: {read_only: [/var/lib/texmf], read_write: [/opt/sandbox-science]}
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

# KillMode=mixed sends the unit alone the stop signal, so it stops the worker, the middleware
# and the cache one after the other; the timeout covers three services' stops. The cache
# wants many open files.
systemd-run --user --unit="$UNIT-unit" --collect -q -p LimitNOFILE=65536 -p KillMode=mixed -p TimeoutStopSec=150 --working-directory="$OS" \
  "$R/.venv/bin/sandbox-science-unit" serve --config "$UNIT_CONFIG"
printf '%s\n' "$WORKER_DIGEST" > "$OS/unit-worker.sha256"
for _ in $(seq 1 240); do
  "$R/.venv/bin/sandbox-science-unit" status --config "$UNIT_CONFIG" >/dev/null 2>&1 && break
  active "$UNIT-unit" || { echo "$UNIT-unit stopped: journalctl --user -u $UNIT-unit" >&2; exit 1; }
  sleep 0.5
done
systemd-run --user --unit="$UNIT-gateway" --collect -q -p KillMode=mixed -p TimeoutStopSec=90 --working-directory="$OS" \
  --setenv=XDG_CONFIG_HOME="$XDG_CONFIG_HOME" --setenv=XDG_STATE_HOME="$XDG_STATE_HOME" --setenv=XDG_DATA_HOME="$XDG_DATA_HOME" \
  --setenv=OPENSHELL_LOCAL_TLS_DIR="$OPENSHELL_LOCAL_TLS_DIR" --setenv=OPENSHELL_TELEMETRY_ENABLED=false \
  "$OS_BIN/openshell-gateway" --config "$OS/gateway.toml"
for _ in $(seq 1 40); do listening "$GATEWAY_PORT" && break; sleep 0.5; done
if ! "$OS_BIN/openshell" gateway list 2>/dev/null | grep -q "127.0.0.1:$GATEWAY_PORT"; then
  "$OS_BIN/openshell" gateway add "https://127.0.0.1:$GATEWAY_PORT" --local --name openshell >/dev/null 2>&1
fi
for part in unit gateway; do
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
echo "OpenShell: $UNIT-unit (cache, middleware, worker) and $UNIT-gateway are running (state in $OS)"
}
$START_OPENSHELL && start_openshell
$OPENSHELL_ONLY && exit 0

# The MCP proxy retired (D98, 2026-10-26): a config that still has [mcp_proxy] is refused by
# the launcher, so say how to migrate it before starting anything that waits for it.
retired=0
"$R/.venv/bin/python" -I -c 'import sys, tomllib; sys.exit(3 if "mcp_proxy" in tomllib.load(open(sys.argv[1], "rb")) else 0)' "$CONFIG" 2>/dev/null || retired=$?
if [ "$retired" -eq 3 ]; then
  echo "$CONFIG has [mcp_proxy], which the launcher refuses since the MCP proxy retired: run" >&2
  echo "  $R/.venv/bin/sandbox-science migrate-mcp-config $CONFIG" >&2
  echo "then this again (docs/MCP-CONNECTORS.md, \"Migrating from [mcp_proxy]\")" >&2
  exit 1
fi
# The launcher, which stops its parts in order on SIGTERM: KillMode=mixed sends it alone the
# signal, and the timeout covers the research sandboxes' suspension.
systemd-run --user --unit="$UNIT-launcher" --collect -q -p KillMode=mixed -p TimeoutStopSec=300 --working-directory="$R" \
  "$R/.venv/bin/sandbox-science" up --config "$CONFIG"
echo "launcher: $UNIT-launcher started; waiting for it to report it runs (the first start imports skills and boots the agent runtime's sandbox)"
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
