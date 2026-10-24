#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Snapshot, check and restore the stores of the owner's personal Sandbox Science on babbage
# (docs/BACKUP-RESTORE.md). The work is scripts/backup_stores.py's; this fills in the
# personal instance's paths and refuses to restore over a running instance.
#
#   scripts/personal_backup.sh [--label NAME] [--live]    # a dated backup, then verified
#   scripts/personal_backup.sh --verify BACKUP            # check a backup again
#   scripts/personal_backup.sh --versions                 # would the checkout migrate a store?
#                                                         # (exit 3: yes; 1: a store is newer)
#   scripts/personal_backup.sh --restore BACKUP [--apply] [--with-config]
#                                                         # say what would change (or do it)
#
# Take one with the instance stopped (scripts/personal_down.sh), and before every update
# that changes a schema version: `--label before-<new revision>`. With the launcher running
# it refuses, unless --live: each store is then consistent in itself, but the stores may
# be at different moments.
#
# What it copies: the control store and its artifact bytes, the worker's SQLite stores and
# content-addressed blobs, the middleware's event store and policy store, and the two
# config files. What it never copies: OpenShell's gateway database and keys
# ($ROOT/openshell/state/openshell), the tokens, the TLS keys, the secrets directory, the
# package cache and the sandboxes' disks.
#
# Settings, from the environment (the defaults are babbage's personal instance):
#   SS_PERSONAL_ROOT     ~/.local/state/sandbox-science-personal
#   SS_PERSONAL_CONFIG   ~/.config/sandbox-science/personal.toml
#   SS_PERSONAL_UNIT     ss-personal   (the units' prefix, to see whether it runs)
#   SS_PERSONAL_BACKUPS  ~/.local/share/sandbox-science-personal-backups
#   R                    the repository (this script's by default)
set -euo pipefail
R=${R:-$(cd "$(dirname "$0")/.." && pwd)}
ROOT=${SS_PERSONAL_ROOT:-$HOME/.local/state/sandbox-science-personal}
CONFIG=${SS_PERSONAL_CONFIG:-$HOME/.config/sandbox-science/personal.toml}
UNIT=${SS_PERSONAL_UNIT:-ss-personal}
BACKUPS=${SS_PERSONAL_BACKUPS:-$HOME/.local/share/sandbox-science-personal-backups}
usage() {
  echo "usage: $0 [--label NAME] [--live] | --versions | --verify BACKUP | --restore BACKUP [--apply] [--with-config]" >&2
  exit 2
}
case "$UNIT" in openshell-lab*) echo "the units' prefix must not be the labs'" >&2; exit 2 ;; esac
case "$ROOT$BACKUPS" in *"/.local/state/openshell"*) echo "the paths must not be the labs'" >&2; exit 2 ;; esac
PY="$R/.venv/bin/python"
[ -x "$PY" ] || PY=python3
TOOL="$R/scripts/backup_stores.py"
LAUNCHER="$ROOT/launcher"
MIDDLEWARE="$ROOT/openshell/middleware/state"
active() { systemctl --user is-active -q "$1" 2>/dev/null; }

mode=backup label=() live=() apply=() with_config=false target=
while [ $# -gt 0 ]; do
  case "$1" in
    --label) [ $# -ge 2 ] || usage; label=(--label "$2"); shift ;;
    --live) live=(--live) ;;
    --versions) mode=versions ;;
    --verify) [ $# -ge 2 ] || usage; mode=verify; target=$2; shift ;;
    --restore) [ $# -ge 2 ] || usage; mode=restore; target=$2; shift ;;
    --apply) apply=(--apply) ;;
    --with-config) with_config=true ;;
    *) usage ;;
  esac
  shift
done

case "$mode" in
  verify) exec "$PY" "$TOOL" verify "$target" ;;
  versions) exec "$PY" "$TOOL" versions --launcher-state "$LAUNCHER" ;;
  restore)
    for part in launcher gateway middleware cache; do
      if active "$UNIT-$part"; then
        echo "$UNIT-$part is running: scripts/personal_down.sh first" >&2; exit 1
      fi
    done
    config=()
    if $with_config; then config=(--config-dir "$(dirname "$CONFIG")"); fi
    exec "$PY" "$TOOL" restore "$target" --launcher-state "$LAUNCHER" \
      --middleware-state "$MIDDLEWARE" "${config[@]}" "${apply[@]}"
    ;;
esac

if active "$UNIT-middleware"; then
  echo "note: $UNIT-middleware runs, so events.db is copied as it is at this moment" >&2
fi
files=()
for file in "$CONFIG" "$(dirname "$CONFIG")/personal-policy.yaml"; do
  if [ -f "$file" ]; then files+=(--config-file "$file"); fi
done
previous=
if [ -d "$BACKUPS" ]; then
  previous=$(find "$BACKUPS" -mindepth 2 -maxdepth 2 -name MANIFEST.json -printf '%h\n' | sort | tail -n 1)
fi
link=()
if [ -n "$previous" ]; then link=(--link-from "$previous"); fi
made=$("$PY" "$TOOL" backup --dest "$BACKUPS" --launcher-state "$LAUNCHER" \
  --middleware-state "$MIDDLEWARE" --repository "$R" "${files[@]}" "${link[@]}" \
  "${label[@]}" "${live[@]}")
"$PY" "$TOOL" verify "$made"
echo "$made"
