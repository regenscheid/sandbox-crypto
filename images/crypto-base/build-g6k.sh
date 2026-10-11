#!/bin/bash
# SPDX-License-Identifier: MIT
# Build G6K from a source tree and install it into the Conda prefix. Run it under
# `micromamba run -n base`, which sets the conda-forge compilers' CC, CXX and flags.
#
# G6K's configure defaults to -march=native, which would tie the image to the build host's CPU.
# --disable-native turns that off and G6K_MARCH names the target instead (x86-64-v3 or armv8.2-a,
# for example); G6K's own optimisation flags are kept.
set -euo pipefail

src=$1
: "${G6K_MARCH:?G6K_MARCH names the target CPU, for example x86-64-v3}"

cd "$src"
autoreconf -i
./configure --disable-native --with-max-sieving-dim=128 \
  CXXFLAGS="${CXXFLAGS:-} -Ofast -ftree-vectorize -funroll-loops -march=${G6K_MARCH}"
echo "G6K kernel CXXFLAGS: $(sed -n 's/^CXXFLAGS = //p' kernel/Makefile)"
make -j"$(nproc)"
python -m pip install --no-deps --no-build-isolation --no-index --no-cache-dir .
cd /  # import the installed package, not the source tree
python -c "from g6k import Siever; print('g6k', Siever)"
