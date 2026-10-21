#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build-only install from the exact signed Ubuntu snapshot; never fall back to live indexes.
set -euo pipefail

snapshot=${1:-}
if [[ ! $snapshot =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || [ "$#" -lt 2 ]; then
  echo "usage: apt_install.sh SNAPSHOT PACKAGE..." >&2
  exit 2
fi
shift
options=(-o "APT::Snapshot=$snapshot" -o Acquire::Retries=5)
# apt update otherwise returns success after an unavailable snapshot and merely warns.
# A later snapshot-constrained install then misleadingly reports missing packages.
apt-get "${options[@]}" -o APT::Update::Error-Mode=any update
apt-get "${options[@]}" install -y --no-install-recommends "$@"
