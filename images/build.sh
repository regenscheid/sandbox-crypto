#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build a base profile image: images/build.sh minimal|crypto-base [docker buildx build options]
#
# Tags sandbox-science/<profile>:<short SHA of the last commit that changed the build's inputs>
# (with -dirty when they have uncommitted changes) and prints the tag, the image digest, its size
# and the build time. It loads the image into the local image store and never pushes: crypto-base
# contains GPL tools and is never published (D46). PLATFORM selects the platform (default
# linux/amd64); BUILDER a buildx builder.
set -euo pipefail

profile=${1:-}
case $profile in
  minimal | crypto-base) shift ;;
  *) echo "usage: images/build.sh minimal|crypto-base [docker buildx build options]" >&2; exit 2 ;;
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
# The recipe's revision: the last commit that changed what the build reads, so that changes to
# the scripts, tests or anything outside images/ leave the tag alone.
inputs=(Containerfile .dockerignore common minimal crypto-base)
revision=$(git -C "$here" log -1 --format=%H -- "${inputs[@]}")
short=$(git -C "$here" rev-parse --short "$revision")
if [ -n "$(git -C "$here" status --porcelain -- "${inputs[@]}")" ]; then
  revision="$revision-dirty"
  short="$short-dirty"
fi
tag="sandbox-science/$profile:$short"
licenses=$(python3 "$here/common/image_report.py" label "$here/$profile/licenses.txt")

metadata=$(mktemp)
trap 'rm -f "$metadata"' EXIT
builder=()
[ -z "${BUILDER:-}" ] || builder=(--builder "$BUILDER")

started=$(date +%s)
docker buildx build "${builder[@]}" \
  --platform "${PLATFORM:-linux/amd64}" \
  --file "$here/Containerfile" \
  --target "$profile" \
  --build-arg REVISION="$revision" \
  --build-arg VERSION="$short" \
  --build-arg LICENSES="$licenses" \
  --tag "$tag" \
  --load \
  --metadata-file "$metadata" \
  "$@" \
  "$here"
elapsed=$(($(date +%s) - started))

digest=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("containerimage.digest", "unknown"))' "$metadata")
size=$(docker image inspect --format '{{.Size}}' "$tag")
echo "image:  $tag"
echo "digest: $digest"
echo "size:   $((size / 1000000)) MB"
echo "built:  in ${elapsed} s"
