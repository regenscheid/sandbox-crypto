# SPDX-License-Identifier: MIT
# Sourced by build.sh and test.sh: a profile's recipe revision, which names its image's tag.
#
# The revision is the last commit that changed what the profile's build reads, with -dirty when
# those files have uncommitted changes. Changes to the scripts, the tests, another profile or
# anything else outside them leave the tag alone.

# Sets inputs: the files the profile's build reads, relative to images/.
recipe_inputs() {
  inputs=(Containerfile .dockerignore common "$1")
  if [ "$1" = orchestrator ]; then
    # build.sh builds the harness's wheels from these packages.
    inputs+=(
      ../packages/contracts/pyproject.toml ../packages/contracts/src
      ../packages/orchestrator/pyproject.toml ../packages/orchestrator/src
    )
  fi
}

# Sets revision (the commit, with -dirty) and short (its short form, the image's tag).
recipe_revision() {
  local images
  images=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
  recipe_inputs "$1"
  revision=$(git -C "$images" log -1 --format=%H -- "${inputs[@]}")
  short=$(git -C "$images" rev-parse --short "$revision")
  if [ -n "$(git -C "$images" status --porcelain -- "${inputs[@]}")" ]; then
    revision="$revision-dirty"
    short="$short-dirty"
  fi
}
