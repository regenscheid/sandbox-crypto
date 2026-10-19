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
