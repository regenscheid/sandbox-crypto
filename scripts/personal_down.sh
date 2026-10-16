#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Stop the owner's personal Sandbox Science on babbage (docs/PERSONAL-BABBAGE.md): the
# launcher first (`sandbox-science down`, which suspends the research sandboxes and deletes
# the MCP proxy's), then its OpenShell gateway, policy middleware and package cache. It
# deletes nothing: the projects, the sandboxes' files and the OpenShell state stay under
# $SS_PERSONAL_ROOT for the next scripts/personal_up.sh.
#
#   scripts/personal_down.sh [--keep-sandboxes]
#
# --keep-sandboxes passes on to `sandbox-science down` and leaves OpenShell running, so the
# research sandboxes and their kernels stay up for the next start. Settings as personal_up.sh.
set -uo pipefail
R=${R:-$(cd "$(dirname "$0")/.." && pwd)}
CONFIG=${SS_PERSONAL_CONFIG:-$HOME/.config/sandbox-science/personal.toml}
UNIT=${SS_PERSONAL_UNIT:-ss-personal}
KEEP=()
case "${1:-}" in
  "") ;;
  --keep-sandboxes) KEEP=(--keep-sandboxes) ;;
  *) echo "usage: $0 [--keep-sandboxes]" >&2; exit 2 ;;
esac
active() { systemctl --user is-active -q "$1" 2>/dev/null; }
status=0

if [ -f "$CONFIG" ]; then
  "$R/.venv/bin/sandbox-science" down --config "$CONFIG" "${KEEP[@]}" || status=$?
fi
for _ in $(seq 1 60); do active "$UNIT-launcher" || break; sleep 1; done
if active "$UNIT-launcher"; then
  echo "the launcher is still running; stopping its unit"
  systemctl --user stop "$UNIT-launcher"
fi
if [ ${#KEEP[@]} -eq 0 ]; then
  systemctl --user stop "$UNIT-gateway" "$UNIT-middleware" "$UNIT-cache" 2>/dev/null
fi
for part in launcher gateway middleware cache; do
  printf '%s-%s: %s\n' "$UNIT" "$part" "$(systemctl --user is-active "$UNIT-$part" 2>/dev/null)"
done
exit "$status"
