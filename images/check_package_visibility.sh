#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Check that the GHCR packages named by the profiles are private (the owner's rule: images
# stay private). Run by .github/workflows/arm64-images.yml with GH_TOKEN and REPOSITORY
# (owner/repo) set.
#
#   check_package_visibility.sh before PROFILE...  a package that does not exist yet passes
#   check_package_visibility.sh after PROFILE...   the package must exist, after a push
#
# Fails naming every package that exists and is not private (or, after a push, is missing).
# The API cannot change visibility: the owner sets a package private in its GitHub package
# settings.
set -u

mode=${1:-}
case "$mode" in before | after) shift ;; *) echo 'usage: check_package_visibility.sh before|after PROFILE...' >&2; exit 2 ;; esac
[ "$#" -gt 0 ] || { echo 'no profiles given' >&2; exit 2; }
: "${REPOSITORY:?REPOSITORY (owner/repo) is required}"
owner=${REPOSITORY%%/*}
repo=${REPOSITORY#*/}

failed=0
for profile in "$@"; do
  package="$repo/$profile"
  if output=$(gh api "/users/$owner/packages/container/${repo}%2F${profile}" --jq .visibility 2>&1); then
    if [ "$output" != private ]; then
      echo "::error::package $package is $output, not private: set it private in its GitHub package settings (the API cannot change it)" >&2
      failed=1
    fi
  else
    case "$output" in
      *"HTTP 404"*)
        if [ "$mode" = after ]; then
          echo "::error::package $package was not found after its push" >&2
          failed=1
        else
          echo "package $package does not exist yet; it is checked again after its first push"
        fi ;;
      *)
        echo "::error::cannot read package $package's visibility: $output" >&2
        failed=1 ;;
    esac
  fi
done
exit "$failed"
