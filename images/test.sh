#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Check a profile image as a sandbox sees it: images/test.sh PROFILE [IMAGE]
# PROFILE is minimal, crypto-base, orchestrator or mcp-proxy.
#
# Runs images/checks/check_image.py in the image as uid 1000, with no capabilities, a read-only
# root filesystem and only the declared paths writable (docs/IMAGES.md, "Tests"):
#   container  tmpfs at /sandbox and /tmp, and for the research profiles a volume for the Conda
#              prefix (D48), as a container driver would run it;
#   landlock   the same under a Landlock ruleset like OpenShell's: its baseline, the devices the
#              policy compiler adds to research sandboxes, and the image's filesystem section
#              (images/common/sandbox-filesystem.yml, or images/<profile>/filesystem.yml for the
#              orchestrator's and the MCP proxy's sandboxes, which get no devices).
# IMAGE defaults to the tag build.sh gives. MODES picks the modes (default
# "container landlock"); NETWORK=0 skips the runtime installs, which need conda-forge and PyPI;
# FILESYSTEM names another filesystem section to try. Prints one JSON line per check and exits 1
# if any failed.
set -euo pipefail

profile=${1:-}
case $profile in
  minimal | crypto-base | orchestrator | mcp-proxy) ;;
  *) echo "usage: images/test.sh minimal|crypto-base|orchestrator|mcp-proxy [IMAGE]" >&2; exit 2 ;;
esac
here=$(cd "$(dirname "$0")" && pwd)
worker="$here/../packages/worker/src/sandbox_science/worker"   # its in-sandbox files
image=${2:-}
if [ -z "$image" ]; then  # the tag build.sh gives
  # shellcheck source=revision.sh
  . "$here/revision.sh"
  recipe_revision "$profile"
  image="sandbox-science/$profile:$short"
fi
docker image inspect "$image" >/dev/null

# OpenShell v0.1.5's baseline for a sandbox whose policy has network rules, and its workdir.
baseline_ro=(/usr /lib /etc /app /var/log /proc /dev/urandom)
baseline_rw=(/tmp /dev/null /sandbox)
# The devices the policy compiler adds (sandbox_science.policy.openshell.SANDBOX_DEVICES_*).
devices_ro=(/dev/random)
devices_rw=(/dev/full /dev/ptmx /dev/pts /dev/shm /dev/tty /dev/zero)
# The image's own paths, as the targets file lists them.
filesystem=${FILESYSTEM:-$here/common/sandbox-filesystem.yml}
# Runtime installs go into the Conda prefix, a writable volume (D48).
prefix=(--mount type=volume,target=/opt/sandbox-science)
case $profile in
  orchestrator | mcp-proxy)
    # compile_orchestrator() and compile_mcp_proxy() add no devices, and the services' prefixes
    # stay read-only.
    devices_ro=()
    devices_rw=()
    filesystem=${FILESYSTEM:-$here/$profile/filesystem.yml}
    prefix=()
    ;;
esac
list() { sed -n "s/^$1: \[\(.*\)\]\$/\1/p" "$filesystem" | tr -d ' ' | tr ',' ' '; }
read -r -a image_ro <<<"$(list read_only)"
read -r -a image_rw <<<"$(list read_write)"

checks=$(mktemp -d)
trap 'rm -rf "$checks"' EXIT
# The worker's in-sandbox files (SANDBOX_FILES in backends.py; a worker test keeps this in step).
cp "$here/checks/check_image.py" "$here/checks/landlock.py" "$checks/"
for name in binding_inventory executor executor_lifecycle provisioning pdfhelper daemon request_frames capture sandbox_files sandbox_processes sandbox_setup; do
  cp "$worker/$name.py" "$checks/"
done
chmod -R a+rX "$checks"

check_args=(--profile "$profile" --worker-files /checks)
network=(--network none)
if [ "${NETWORK:-1}" != 0 ]; then
  check_args+=(--network)
  network=()
fi

landlock=(python -I /checks/landlock.py)
for path in "${baseline_ro[@]}" "${devices_ro[@]}" "${image_ro[@]}" /checks; do landlock+=(--ro "$path"); done
for path in "${baseline_rw[@]}" "${devices_rw[@]}" "${image_rw[@]}"; do landlock+=(--rw "$path"); done
landlock+=(--)

status=0
for mode in ${MODES:-container landlock}; do
  case $mode in
    container) wrapper=() mode_args=() ;;
    landlock) wrapper=("${landlock[@]}") mode_args=(--landlock) ;;
    *) echo "test.sh: unknown mode $mode" >&2; exit 2 ;;
  esac
  echo "== $image ($mode)" >&2
  docker run --rm --name "ss-image-test-$profile-$mode-$$" \
    --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges \
    --read-only \
    --tmpfs /sandbox:rw,exec,uid=1000,gid=1000,mode=0755,size=8g \
    --tmpfs /tmp:rw,exec,mode=1777,size=4g \
    --shm-size 1g \
    "${prefix[@]}" \
    --mount "type=bind,source=$checks,target=/checks,readonly" \
    "${network[@]}" \
    "$image" \
    "${wrapper[@]}" python -I /checks/check_image.py "${check_args[@]}" "${mode_args[@]}" |
    sed "s/^{/{\"mode\": \"$mode\", /" || status=1
done
exit $status
