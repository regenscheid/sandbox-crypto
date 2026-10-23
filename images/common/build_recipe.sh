#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Shared inputs for local image builds and qualified registry publication.

prepare_image_build() {
  local profile=$1 package
  image_here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
  # shellcheck source=../revision.sh
  . "$image_here/revision.sh"
  recipe_revision "$profile"
  image_licenses=$(python3 "$image_here/common/image_report.py" label "$image_here/$profile/licenses.txt")
  image_work=$(mktemp -d)
  trap 'rm -rf "$image_work"' EXIT

  image_builder=()
  [ -z "${BUILDER:-}" ] || image_builder=(--builder "$BUILDER")
  if [ -n "${PLATFORM:-}" ]; then
    image_platform=$PLATFORM
  else
    case $(uname -m) in
      x86_64 | amd64) image_platform=linux/amd64 ;;
      aarch64 | arm64) image_platform=linux/arm64 ;;
      *) echo "image build: unsupported host architecture; set PLATFORM explicitly" >&2; return 2 ;;
    esac
  fi
  case $image_platform in
    linux/amd64 | linux/arm64) ;;
    *) echo "image build: PLATFORM must be linux/amd64 or linux/arm64" >&2; return 2 ;;
  esac

  image_contexts=(--build-context "binding-inventory=$image_here/../packages/worker/src/sandbox_science/worker")
  recipe_packages "$profile"
  if [ ${#packages[@]} -gt 0 ]; then
    for package in "${packages[@]}"; do
      uv --directory "$image_here/.." build --package "sandbox-science-$package" --wheel \
        --no-create-gitignore --out-dir "$image_work/wheels"
    done
    image_contexts+=(--build-context "wheels=$image_work/wheels")
  fi
  image_args=("${image_builder[@]}" --platform "$image_platform" \
    --file "$image_here/Containerfile" --target "$profile" \
    "${image_contexts[@]}" \
    --build-arg "REVISION=$revision" --build-arg "VERSION=$short" \
    --build-arg "LICENSES=$image_licenses")
}
