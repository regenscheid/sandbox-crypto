#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Smoke-test an existing local trusted image; never pull, publish or contact services.
set -euo pipefail
profile=${1:-}
case $profile in
  trusted-app|trusted-worker) ;;
  *) echo "usage: images/test_trusted.sh trusted-app|trusted-worker [LOCAL_IMAGE]" >&2; exit 2 ;;
esac
[ "$#" -le 2 ] || { echo "test_trusted.sh: too many arguments" >&2; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=revision.sh
. "$here/revision.sh"
recipe_revision "$profile"
reference=${2:-sandbox-science/$profile:$short}
if [[ ! $reference =~ ^[A-Za-z0-9][A-Za-z0-9./:@_-]*$ ]]; then
  echo "test_trusted.sh: invalid local image reference" >&2
  exit 2
fi
docker run --rm --pull=never --network=none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges --pids-limit=128 --memory=1g --cpus=2 \
  --tmpfs /tmp:rw,nosuid,nodev,size=64m --tmpfs /sandbox:rw,nosuid,nodev,size=64m,uid=1000,gid=1000 \
  --mount "type=bind,source=$here/trusted/check.py,target=/run/check.py,readonly" \
  --user 1000:1000 --entrypoint python "$reference" -B /run/check.py "$profile"
