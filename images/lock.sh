#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Solve the profiles' Conda lockfiles again: images/lock.sh [PROFILE ...]
# PROFILE is minimal, crypto-base or orchestrator; without one, every profile.
#
# Each images/<profile>/conda-lock.yml pins every package, with its SHA-256, for linux-64 and
# linux-aarch64, as conda-lock solves images/<profile>/environment.yml. conda-lock runs from a
# throwaway environment (uvx), or the executable CONDA_LOCK names, and solves with the micromamba
# release the Containerfile pins, checked against the same SHA-256. Review the diff, rebuild,
# and update licenses.txt if the build asks for it.
set -euo pipefail

CONDA_LOCK_VERSION=4.0.2

here=$(cd "$(dirname "$0")" && pwd)
if [ -n "${CONDA_LOCK:-}" ]; then
  conda_lock=("$CONDA_LOCK")
else
  conda_lock=(uvx --from "conda-lock==$CONDA_LOCK_VERSION" conda-lock)
fi
"${conda_lock[@]}" --version | grep -q "version $CONDA_LOCK_VERSION\$" ||
  echo "lock.sh: warning: not conda-lock $CONDA_LOCK_VERSION" >&2
profiles=("$@")
[ ${#profiles[@]} -gt 0 ] || profiles=(minimal crypto-base orchestrator)

arg() { sed -n "s/^ARG $1=//p" "$here/Containerfile"; }
version=$(arg MICROMAMBA_VERSION)
case $(uname -m) in
  x86_64) asset=micromamba-linux-64 sha256=$(arg MICROMAMBA_SHA256_AMD64) ;;
  aarch64) asset=micromamba-linux-aarch64 sha256=$(arg MICROMAMBA_SHA256_ARM64) ;;
  *) echo "lock.sh: no pinned micromamba for $(uname -m)" >&2; exit 1 ;;
esac

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fsSL -o "$work/micromamba" \
  "https://github.com/mamba-org/micromamba-releases/releases/download/$version/$asset"
echo "$sha256  $work/micromamba" | sha256sum --check --quiet
chmod +x "$work/micromamba"

for profile in "${profiles[@]}"; do
  [ -f "$here/$profile/environment.yml" ] || { echo "lock.sh: no profile $profile" >&2; exit 1; }
  echo "== $profile"
  rm -f "$here/$profile/conda-lock.yml"
  (
    cd "$here/$profile"
    MAMBA_ROOT_PREFIX="$work/root" "${conda_lock[@]}" lock \
      --micromamba --conda "$work/micromamba" \
      --file environment.yml \
      --virtual-package-spec ../common/virtual-packages.yml \
      --lockfile conda-lock.yml
  )
done
