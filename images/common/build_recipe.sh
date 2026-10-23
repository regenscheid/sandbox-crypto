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

# Adds the trusted service targets' extra inputs to image_args: the hashed requirements exported
# from uv.lock for the profile's packages and the narrow named build contexts. Local builds
# (build_trusted.sh) and registry publication (publish_arch.sh) share it, so both build the
# same image from the same inputs.
prepare_trusted_inputs() {
  local profile=$1 package
  local requirements=()
  for package in "${packages[@]}"; do
    requirements+=(--package "sandbox-science-$package")
  done
  requirements+=(--extra s3)
  uv --directory "$image_here/.." export --locked --no-dev --no-emit-workspace \
    --no-header --no-annotate "${requirements[@]}" \
    --output-file "$image_work/requirements.txt" >/dev/null
  image_args+=(--build-context "trusted-inputs=$image_work"
    --build-context "workspace-web=$image_here/../web/workspace"
    --build-context "repository-tools=$image_here/../scripts")
  if [ "$profile" = trusted-app ]; then
    # The notice generator reads ../../LICENSE from web/workspace. Send that exact
    # authoritative file without exposing the full checkout as a Docker context.
    mkdir "$image_work/repository-license"
    cp "$image_here/../LICENSE" "$image_work/repository-license/LICENSE"
    image_args+=(--build-context "repository-license=$image_work/repository-license")
  fi
}
