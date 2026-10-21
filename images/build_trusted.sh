#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build an unpublished trusted service image. No caller-supplied Buildx output options.
set -euo pipefail

if [ "$#" != 1 ]; then
  echo "usage: images/build_trusted.sh trusted-app|trusted-worker (local load only)" >&2
  exit 2
fi
case $1 in
  trusted-app|trusted-worker) profile=$1 ;;
  *) echo "build_trusted.sh: only unpublished trusted service profiles are supported" >&2; exit 2 ;;
esac
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=common/build_recipe.sh
. "$here/common/build_recipe.sh"
prepare_image_build "$profile"

requirements=()
for package in "${packages[@]}"; do
  requirements+=(--package "sandbox-science-$package")
done
requirements+=(--extra s3)
uv --directory "$here/.." export --locked --no-dev --no-emit-workspace \
  --no-header --no-annotate "${requirements[@]}" \
  --output-file "$image_work/requirements.txt" >/dev/null
image_args+=(--build-context "trusted-inputs=$image_work"
  --build-context "workspace-web=$here/../web/workspace"
  --build-context "repository-tools=$here/../scripts")
if [ "$profile" = trusted-app ]; then
  # The notice generator reads ../../LICENSE from web/workspace. Send that exact
  # authoritative file without exposing the full checkout as a Docker context.
  mkdir "$image_work/repository-license"
  cp "$here/../LICENSE" "$image_work/repository-license/LICENSE"
  image_args+=(--build-context "repository-license=$image_work/repository-license")
fi
tag="sandbox-science/$profile:$short"
docker buildx build "${image_args[@]}" --tag "$tag" --load \
  --metadata-file "$image_work/metadata.json" "$here"
docker image inspect --format '{{.Id}} {{.Size}}' "$tag"
printf 'local image: %s\n' "$tag"
