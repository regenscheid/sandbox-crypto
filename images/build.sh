#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build a profile image: images/build.sh PROFILE [docker buildx build options]
# PROFILE is minimal, crypto-base or orchestrator.
#
# Tags sandbox-science/<profile>:<short SHA of the last commit that changed the build's inputs>
# (with -dirty when they have uncommitted changes; revision.sh) and prints the tag, the image
# digest, its size and the build time. It loads the image into the local image store and never
# pushes: crypto-base contains GPL tools and can only be copied to a verified private registry
# under D46. For orchestrator it first builds the wheels of the repository's packages it
# installs from this checkout with uv. PLATFORM selects the platform (default: the host's Linux architecture);
# BUILDER selects a buildx builder.
set -euo pipefail

profile=${1:-}
case $profile in
  minimal | crypto-base | orchestrator) shift ;;
  *)
    echo "usage: images/build.sh minimal|crypto-base|orchestrator" \
      "[docker buildx build options]" >&2
    exit 2
    ;;
esac
for option in "$@"; do
  case $option in
    --push* | *type=registry* | *push=true*)
      echo "build.sh: refusing to push; images are only loaded locally" >&2
      exit 2
      ;;
  esac
done

here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=common/build_recipe.sh
. "$here/common/build_recipe.sh"
prepare_image_build "$profile"
tag="sandbox-science/$profile:$short"
started=$(date +%s)
docker buildx build "${image_args[@]}" \
  --tag "$tag" \
  --load \
  --metadata-file "$image_work/metadata.json" \
  "$@" \
  "$here"
elapsed=$(($(date +%s) - started))

digest=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("containerimage.digest", "unknown"))' "$image_work/metadata.json")
size=$(docker image inspect --format '{{.Size}}' "$tag")
echo "image:  $tag"
echo "digest: $digest"
echo "size:   $((size / 1000000)) MB"
echo "built:  in ${elapsed} s"
