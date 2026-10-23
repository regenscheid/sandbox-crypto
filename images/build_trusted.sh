#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build a trusted service image into the local Docker store. No caller-supplied Buildx output
# options; registry publication is images/publish_arch.sh, run by the manual image workflow.
set -euo pipefail

if [ "$#" != 1 ]; then
  echo "usage: images/build_trusted.sh trusted-app|trusted-worker (local load only; publication is publish_arch.sh)" >&2
  exit 2
fi
case $1 in
  trusted-app|trusted-worker) profile=$1 ;;
  *) echo "build_trusted.sh: only the trusted service profiles are supported" >&2; exit 2 ;;
esac
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=common/build_recipe.sh
. "$here/common/build_recipe.sh"
prepare_image_build "$profile"

prepare_trusted_inputs "$profile"
tag="sandbox-science/$profile:$short"
docker buildx build "${image_args[@]}" --tag "$tag" --load \
  --metadata-file "$image_work/metadata.json" "$here"
docker image inspect --format '{{.Id}} {{.Size}}' "$tag"
printf 'local image: %s\n' "$tag"
