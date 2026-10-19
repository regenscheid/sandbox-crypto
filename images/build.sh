#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build a profile image: images/build.sh PROFILE [docker buildx build options]
# PROFILE is minimal, crypto-base, orchestrator or mcp-proxy.
#
# Tags sandbox-science/<profile>:<short SHA of the last commit that changed the build's inputs>
# (with -dirty when they have uncommitted changes; revision.sh) and prints the tag, the image
# digest, its size and the build time. It loads the image into the local image store and never
# pushes: crypto-base contains GPL tools and can only be copied to a verified private registry
# under D46. For orchestrator and
# mcp-proxy it first builds the wheels of the repository's packages they install from this
# checkout with uv. PLATFORM selects the platform (default: the host's Linux architecture);
# BUILDER selects a buildx builder.
set -euo pipefail

profile=${1:-}
case $profile in
  minimal | crypto-base | orchestrator | mcp-proxy) shift ;;
  *)
    echo "usage: images/build.sh minimal|crypto-base|orchestrator|mcp-proxy" \
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
# shellcheck source=revision.sh
. "$here/revision.sh"
recipe_revision "$profile"
tag="sandbox-science/$profile:$short"
licenses=$(python3 "$here/common/image_report.py" label "$here/$profile/licenses.txt")

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
builder=()
[ -z "${BUILDER:-}" ] || builder=(--builder "$BUILDER")
if [ -n "${PLATFORM:-}" ]; then
  platform=$PLATFORM
else
  case $(uname -m) in
    x86_64 | amd64) platform=linux/amd64 ;;
    aarch64 | arm64) platform=linux/arm64 ;;
    *) echo "build.sh: unsupported host architecture; set PLATFORM explicitly" >&2; exit 2 ;;
  esac
fi
case $platform in
  linux/amd64 | linux/arm64) ;;
  *) echo "build.sh: PLATFORM must be linux/amd64 or linux/arm64" >&2; exit 2 ;;
esac

started=$(date +%s)
contexts=(--build-context "binding-inventory=$here/../packages/worker/src/sandbox_science/worker")
recipe_packages "$profile"
if [ ${#packages[@]} -gt 0 ]; then
  # The packages the image installs, as wheels, from the same files the revision covers.
  for package in "${packages[@]}"; do
    uv --directory "$here/.." build --package "sandbox-science-$package" --wheel \
      --no-create-gitignore --out-dir "$work/wheels"
  done
  contexts+=(--build-context "wheels=$work/wheels")
fi
docker buildx build "${builder[@]}" \
  --platform "$platform" \
  --file "$here/Containerfile" \
  --target "$profile" \
  "${contexts[@]}" \
  --build-arg REVISION="$revision" \
  --build-arg VERSION="$short" \
  --build-arg LICENSES="$licenses" \
  --tag "$tag" \
  --load \
  --metadata-file "$work/metadata.json" \
  "$@" \
  "$here"
elapsed=$(($(date +%s) - started))

digest=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("containerimage.digest", "unknown"))' "$work/metadata.json")
size=$(docker image inspect --format '{{.Size}}' "$tag")
echo "image:  $tag"
echo "digest: $digest"
echo "size:   $((size / 1000000)) MB"
echo "built:  in ${elapsed} s"
