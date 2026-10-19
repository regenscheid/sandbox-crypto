# SPDX-License-Identifier: MIT
"""Keep G6K's x86 intrinsics out of its portable ARM64 SIMD build.

The pinned G6K source provides a generic vector path but includes immintrin.h
unconditionally in two files. Fail if that source changes, so this adjustment
cannot silently apply to an unreviewed upstream revision.
"""

import sys
from pathlib import Path


def replace_once(path: Path, before: str, after: str) -> None:
    source = path.read_text()
    if source.count(before) != 1:
        raise SystemExit(f"unexpected G6K source at {path}")
    path.write_text(source.replace(before, after, 1))


root = Path(sys.argv[1])
replace_once(
    root / "kernel/simd.h",
    "#include <immintrin.h>\n",
    "#ifdef HAVE_AVX2\n#include <immintrin.h>\n#endif\n",
)
replace_once(root / "kernel/bdgl_sieve.cpp", "#include <immintrin.h>\n", "")

# GCC 15's AArch64 argument ABI rejects user-aligned vector typedefs when the
# generic SIMD functions pass them by value. Their natural vector alignment is
# sufficient for this architecture, and all G6K objects are rebuilt together.
header = root / "kernel/simd.h"
source = header.read_text()
if source.count(", aligned(16)") != 10:
    raise SystemExit(f"unexpected G6K vector typedefs at {header}")
header.write_text(source.replace(", aligned(16)", ""))
