// SPDX-License-Identifier: MIT
import { copyFile } from 'node:fs/promises';

// These notices accompany the standalone bundle when it is distributed.
await copyFile(new URL('../node_modules/katex/LICENSE', import.meta.url),
  new URL('../dist-math/LICENSE-KaTeX.txt', import.meta.url));
await copyFile(new URL('../../../LICENSE', import.meta.url),
  new URL('../dist-math/LICENSE-Sandbox-Science.txt', import.meta.url));
await copyFile(new URL('check-math.mjs', import.meta.url),
  new URL('../dist-math/check-math.mjs', import.meta.url));
