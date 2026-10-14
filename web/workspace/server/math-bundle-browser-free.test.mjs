// SPDX-License-Identifier: MIT
// The math checker's real bundle graph holds no browser-only module, however it is reached:
// an import, a re-export or a bare import. src/lib/mathcheck.test.ts walks the sources too.
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test } from 'vitest';
import { build } from 'vite';

const root = fileURLToPath(new URL('..', import.meta.url));
// The browser's client and model repairs; and the repair ladder with its Unicode tables,
// which the checker does not use.
const NOT_BUNDLED = [
  'api',
  'http',
  'session',
  'modelRepairs',
  'pageRepairs',
  'mathrepair',
  'texUnicode',
];

test('the math bundle contains no browser-only module, nor the repair ladder', async () => {
  const result = await build({
    root,
    logLevel: 'silent',
    configFile: resolve(root, 'vite.math.config.ts'),
    build: { write: false },
  });
  const output = (Array.isArray(result) ? result.flatMap((r) => r.output) : result.output).filter(
    (item) => item.type === 'chunk',
  );
  const ids = output.flatMap((chunk) => chunk.moduleIds).map((id) => id.replaceAll('\\', '/'));
  expect(ids.some((id) => id.endsWith('/src/lib/mathcheck.ts'))).toBe(true);
  for (const name of NOT_BUNDLED)
    expect(ids.filter((id) => id.endsWith(`/src/lib/${name}.ts`))).toEqual([]);
  for (const chunk of output) expect(chunk.code).not.toContain('/api/v0');
}, 60000);
