#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build a native platform image, push it under a run-unique tag, then test the pushed digest.
# Usage: publish_arch.sh PROFILE GHCR_REPOSITORY RUN_ID
set -euo pipefail

profile=${1:-}
repository=${2:-}
run_token=${3:-}
case $profile in
  minimal | crypto-base | orchestrator | trusted-app | trusted-worker) ;;
  *) echo "publish_arch.sh: unknown profile" >&2; exit 2 ;;
esac
case $repository in
  ghcr.io/*/"$profile") ;;
  *) echo "publish_arch.sh: expected a GHCR repository ending in /$profile" >&2; exit 2 ;;
esac
if [[ ! $run_token =~ ^[0-9]+$ ]]; then
  echo "publish_arch.sh: expected RUN_ID" >&2
  exit 2
fi
if [ "$profile" = crypto-base ] && [ "${PRIVATE_REGISTRY_CONFIRMED:-}" != 1 ]; then
  echo "publish_arch.sh: confirm that the crypto-base package is private" >&2
  exit 2
fi
if [ -z "${BUILDER:-}" ]; then
  echo "publish_arch.sh: set BUILDER to a docker-container Buildx builder" >&2
  exit 2
fi
builder_driver=$(docker buildx inspect "$BUILDER" | awk '/^Driver:/ { print $2 }')
if [ "$builder_driver" != docker-container ]; then
  echo "publish_arch.sh: BUILDER must use the docker-container driver (got $builder_driver)" >&2
  exit 2
fi

here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=common/build_recipe.sh
. "$here/common/build_recipe.sh"
prepare_image_build "$profile"
case $revision in
  *-dirty) echo "publish_arch.sh: refusing a dirty recipe" >&2; exit 2 ;;
esac
case $image_platform in
  linux/amd64) arch=amd64 ;;
  linux/arm64) arch=arm64 ;;
esac
case "$(uname -m):$arch" in
  x86_64:amd64 | aarch64:arm64) ;;
  *) echo "publish_arch.sh: the build must run on native $arch hardware" >&2; exit 2 ;;
esac

case $profile in
  trusted-app | trusted-worker) prepare_trusted_inputs "$profile" ;;
esac

tag="$repository:$short-$arch-run$run_token"
docker buildx build "${image_args[@]}" --tag "$tag" --push \
  --metadata-file "$image_work/metadata.json" "$image_here"
digest=$(python3 - "$image_work/metadata.json" <<'PY'
import json
import re
import sys

digest = json.load(open(sys.argv[1], encoding="utf-8")).get("containerimage.digest", "")
if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
    raise SystemExit("publish_arch.sh: pushed image has no SHA-256 digest")
print(digest)
PY
)
reference="$repository@$digest"
docker buildx imagetools inspect "$reference" >/dev/null
docker pull "$reference"
case $profile in
  trusted-app | trusted-worker) "$here/test_trusted.sh" "$profile" "$reference" ;;
  *) NETWORK=0 "$here/test.sh" "$profile" "$reference" ;;
esac
printf 'qualified image: %s\n' "$reference"
