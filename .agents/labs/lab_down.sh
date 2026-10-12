#!/usr/bin/env bash
# Tear the babbage lab down completely and check that nothing is left.
set -uo pipefail
LAB="$HOME/.local/state/openshell-lab"
if [ -f "$LAB/env.sh" ]; then
  # shellcheck source=/dev/null
  source "$LAB/env.sh"
  openshell sandbox delete --all >/dev/null 2>&1
  for _ in $(seq 1 60); do
    n=$(openshell sandbox list 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | grep -c -v -E '^NAME|^$|No sandboxes')
    [ "$n" = 0 ] && break; sleep 1
  done
  echo "sandboxes left: ${n:-0}"
fi
systemctl --user stop openshell-lab-workspace openshell-lab-mcp-forward openshell-lab-worker openshell-lab-gateway openshell-lab-middleware openshell-lab-cache openshell-lab-echo 2>/dev/null
sleep 2
[ -d "$LAB" ] && echo "removing $(du -sh "$LAB" | cut -f1) in $LAB" && rm -rf -- "$LAB"
ls -d /tmp/os-1000-* >/dev/null 2>&1 && echo "driver socket dirs left in /tmp" || echo "no driver socket dirs in /tmp"
ss -ltn | grep -E ':(8090|8080|8100|8101|50051|17670)\b' || echo "lab ports free"
pgrep -u "$USER" -a -f 'openshell|sandbox-science-(policy-middleware|package-cache|worker|workspace)|echo_server.py|executor.py --stdio' | grep -v pgrep || echo "no lab processes"
compgen -G "$HOME/.config/openshell*" >/dev/null || compgen -G "$HOME/.local/state/openshell*" >/dev/null || compgen -G "$HOME/.local/share/openshell*" >/dev/null \
  && echo "OpenShell paths remain" || echo "no OpenShell paths under ~/.config, ~/.local/state, ~/.local/share"
