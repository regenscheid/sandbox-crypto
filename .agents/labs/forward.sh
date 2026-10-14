#!/usr/bin/env bash
# The MCP proxy's forward as a unit like examples/systemd/sandbox-science-mcp-proxy-forward@.service
# (Restart=always, RestartSec=2s, no start limit), across a stop and start of the proxy's sandbox.
set -uo pipefail
L=$(cd "$(dirname "$0")" && pwd); R=${R:-$(cd "$L/../.." && pwd)}
source "$HOME/.local/state/openshell-lab/env.sh"
W=$LAB/lab17-proxy; N=ss-lab17-proxy; IMAGE=sandbox-science/mcp-proxy:d27e41a
mkdir -p "$W/mcp-proxy" && chmod 700 "$W"
ORCH_TOKEN="test-secret-lab17-orchestrator-$(openssl rand -hex 12)"
(umask 077; printf 'ORCH_TOKEN=%s\n' "$ORCH_TOKEN" > "$W/tokens.env")
ORCH_SHA=$(printf '%s' "$ORCH_TOKEN" | sha256sum | cut -d' ' -f1)
cp "$L/policy-proxy.yaml" "$W/mcp-proxy/policy.yaml"
printf '%s\n' 'apiVersion: sandbox-science/v0alpha1' 'kind: PoolCeiling' > "$W/mcp-proxy/pool-ceiling.yaml"
cat > "$W/mcp-proxy/proxy.toml" <<TOML
listen = "127.0.0.1:8080"
state_dir = "state"
ceiling = "pool-ceiling.yaml"
upstream_timeout = "60s"

[[callers]]
name = "orchestrator"
token_sha256 = "$ORCH_SHA"
kind = "orchestrator"
project = "lab17"
TOML
"$R/.venv/bin/sandbox-science-mcp-proxy" compile-policy --policy "$L/policy-proxy.yaml" \
  --ceiling "$W/mcp-proxy/pool-ceiling.yaml" --targets "$R/examples/openshell/targets.yaml" \
  --out "$W/openshell-policy.yaml"
phase_of() { openshell sandbox get $N -o json 2>/dev/null | python3 -I -c 'import json,sys; print(json.load(sys.stdin).get("phase",""))' 2>/dev/null; }
call() { timeout 60 "$R/.venv/bin/python" -I "$L/mcp_caller.py" http://127.0.0.1:18080 "$ORCH_TOKEN" 2>&1 | tail -1 | cut -c1-110; }
unit() { echo "$(systemctl --user is-active openshell-lab-mcp-forward), $(systemctl --user show -p NRestarts --value openshell-lab-mcp-forward) restarts"; }
openshell sandbox create --name $N --from $IMAGE --policy "$W/openshell-policy.yaml" --no-auto-providers --detach -- /opt/sandbox-science/bin/mcp-proxy-run 2>&1 | tail -1
for _ in $(seq 1 180); do [ "$(phase_of)" = Ready ] && break; sleep 1; done; echo "phase: $(phase_of)"
openshell sandbox upload --no-git-ignore $N "$W/mcp-proxy" /sandbox/ 2>&1 | tail -1
openshell sandbox exec -n $N --no-tty --no-login-shell -- sh -c 'cd /sandbox/mcp-proxy && /opt/sandbox-science/bin/sandbox-science-mcp-proxy bind --state-dir state --project lab17 --policy policy.yaml' 2>&1 | tail -1
systemd-run --user --unit=openshell-lab-mcp-forward --collect -q -p Restart=always -p RestartSec=2s -p StartLimitIntervalSec=0 \
  --working-directory="$W" "${OS_RUN_ENV[@]}" "$OS_BIN/openshell" forward service --target-port 8080 --local 127.0.0.1:18080 $N
for _ in $(seq 1 30); do ss -ltn | grep -q '127.0.0.1:18080' && break; sleep 1; done
sleep 3; echo "-- first call: $(call)"; echo "forward unit: $(unit)"
openshell sandbox stop $N 2>&1 | tail -1
for _ in $(seq 1 90); do [ "$(phase_of)" = Stopped ] && break; sleep 1; done
echo "phase: $(phase_of); forward unit: $(unit)"; sleep 10; echo "10 s later: forward unit: $(unit)"
t0=$(date +%s); openshell sandbox start $N 2>&1 | tail -1
for _ in $(seq 1 120); do [ "$(phase_of)" = Ready ] && break; sleep 1; done; echo "phase: $(phase_of) after $(( $(date +%s) - t0 )) s; forward unit: $(unit)"
for i in $(seq 1 30); do out=$(call); case "$out" in *" 200 "*) break;; esac; sleep 2; done
echo "-- a call succeeded $(( $(date +%s) - t0 )) s after start, attempt $i: $out"; echo "forward unit: $(unit)"
journalctl --user -u openshell-lab-mcp-forward --no-pager -o cat 2>/dev/null | grep -v -i token | sort | uniq -c | sort -rn | head -6 | cut -c1-200
