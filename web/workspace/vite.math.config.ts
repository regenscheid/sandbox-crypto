// SPDX-License-Identifier: MIT
import { defineConfig } from 'vite';

// A standalone Node library: the exact GUI KaTeX and deterministic repair code.
export default defineConfig({
  build: {
    target: 'node22',
    outDir: 'dist-math',
    lib: { entry: 'src/lib/mathcheck.ts', formats: ['es'], fileName: () => 'mathcheck.mjs' },
    sourcemap: false,
    minify: false,
    rolldownOptions: { output: { comments: { legal: true } } },
  },
});
