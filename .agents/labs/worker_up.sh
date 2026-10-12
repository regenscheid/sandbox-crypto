#!/usr/bin/env bash
# After lab_up.sh: the worker from the repository (R), with a client limited to project $PROJECT
# (default lab17) and another limited to ${PROJECT}b. Synthetic tokens only.
set -euo pipefail
R=${R:-$(cd "$(dirname "$0")/../.." && pwd)}
PROJECT=${PROJECT:-lab17}
source "$HOME/.local/state/openshell-lab/env.sh"
W="$LAB/worker"; mkdir -p "$W" "$LAB/orch"
cat > "$LAB/targets.yaml" <<'YAML'
apiVersion: sandbox-science/v0alpha1
kind: OpenShellTargets
sandbox:
  middleware: sandbox-science-policy
  package_cache: {host: host.openshell.internal, port: 8090}
  mcp_proxy: {host: host.openshell.internal, port: 8080}
  binaries: [/**]
  filesystem: {read_write: [/opt/sandbox-science]}
mcp_proxy:
  binaries: [/opt/sandbox-science/bin/python*]
  versions: ["2025-11-25"]
YAML
cd "$W"
"$R/.venv/bin/sandbox-science-worker" token --out worker.token >/dev/null
A_LINE=$("$R/.venv/bin/sandbox-science-worker" token --out client-a.token)
B_LINE=$("$R/.venv/bin/sandbox-science-worker" token --out client-b.token)
cat > worker.toml <<TOML
listen = "127.0.0.1:8100"
token_file = "worker.token"
state_dir = "state"
pool_id = "babbage"

[openshell]
cli = "$OS_BIN/openshell"
python = "/opt/sandbox-science/bin/python"
isolation_tier = 3
package_cache = "http://host.openshell.internal:8090"
env = {XDG_CONFIG_HOME = "$XDG_CONFIG_HOME", XDG_STATE_HOME = "$XDG_STATE_HOME", XDG_DATA_HOME = "$XDG_DATA_HOME", OPENSHELL_LOCAL_TLS_DIR = "$OPENSHELL_LOCAL_TLS_DIR", OPENSHELL_TELEMETRY_ENABLED = "false"}

[openshell.environments]
policy_store = "$LAB/middleware/state"
targets = "$LAB/targets.yaml"
ceiling = "$LAB/middleware/pool-ceiling.yaml"
profiles = {minimal = "sandbox-science/minimal:02f85b3", crypto-base = "sandbox-science/crypto-base:02f85b3"}

[[clients]]
name = "orchestrator-$PROJECT"
$A_LINE
projects = ["$PROJECT"]

[[clients]]
name = "orchestrator-${PROJECT}b"
$B_LINE
projects = ["${PROJECT}b"]
TOML
"$R/.venv/bin/sandbox-science-worker" policy add --settings worker.toml --project "$PROJECT" "$R/examples/policy.yaml" 2>&1 | grep -oE "sha256:[0-9a-f]{64}" | head -1 > "$LAB/policy.digest"
systemd-run --user --unit=openshell-lab-worker --collect -q --working-directory="$W" "$R/.venv/bin/sandbox-science-worker" serve --settings worker.toml --log-level info
for _ in $(seq 1 40); do ss -ltn | grep -q '127.0.0.1:8100' && break; sleep 0.5; done
echo "worker $(systemctl --user is-active openshell-lab-worker), policy $(cat "$LAB/policy.digest")"
