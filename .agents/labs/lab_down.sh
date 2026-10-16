#!/usr/bin/env bash
# Tear the babbage lab down completely and check that nothing is left.
# The owner's personal instance (scripts/personal_up.sh: units ss-personal-*, state under
# ~/.local/state/sandbox-science-personal, docs/PERSONAL-BABBAGE.md) is not the lab's. Nothing
# here stops or removes it, and the checks below leave out its processes and the driver
# socket directory they hold, so that its being up never reads as a lab left behind.
set -uo pipefail
LAB="$HOME/.local/state/openshell-lab"
personal() { grep -q '/ss-personal-' "/proc/$1/cgroup" 2>/dev/null; }
held_by_personal() {
  for pid in $(pgrep -u "$USER"); do
    personal "$pid" && ls -l "/proc/$pid/fd" 2>/dev/null | grep -oE '/tmp/os-[0-9]+-[0-9a-f]+'
  done | sort -u
}
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
held=$(held_by_personal)
left=0
for dir in /tmp/os-"$(id -u)"-*; do
  [ -d "$dir" ] || continue
  grep -qxF -- "$dir" <<<"$held" || left=$((left + 1))
done
[ "$left" != 0 ] && echo "driver socket dirs left in /tmp" || echo "no driver socket dirs in /tmp"
ss -ltn | grep -E ':(8090|8080|8100|8101|50051|17670)\b' || echo "lab ports free"
procs=$(pgrep -u "$USER" -f 'openshell|sandbox-science-(policy-middleware|package-cache|worker|workspace)|echo_server.py|executor.py --stdio' | while read -r pid; do
  [ "$pid" = "$$" ] || personal "$pid" || ps -o pid=,args= -p "$pid" 2>/dev/null | grep -v pgrep
done)
[ -n "$procs" ] && echo "$procs" || echo "no lab processes"
compgen -G "$HOME/.config/openshell*" >/dev/null || compgen -G "$HOME/.local/state/openshell*" >/dev/null || compgen -G "$HOME/.local/share/openshell*" >/dev/null \
  && echo "OpenShell paths remain" || echo "no OpenShell paths under ~/.config, ~/.local/state, ~/.local/share"
