// SPDX-License-Identifier: MIT
// The figure frame's bundle (docs/specs/figures.md, FG-14): its own code only, none of the
// workspace's client and no third-party code yet; nothing that evaluates generated code,
// which its CSP would refuse anyway; and every asset a file of its own.
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test } from 'vitest';
import { build } from 'vite';

const root = fileURLToPath(new URL('..', import.meta.url));

async function figuresBuild() {
  const result = await build({
    logLevel: 'silent',
    configFile: resolve(root, 'vite.figures.config.ts'),
    build: { write: false },
  });
  return Array.isArray(result) ? result.flatMap((r) => r.output) : result.output;
}

test('the figure frame holds its own code, and nothing that reaches the workspace', async () => {
  const output = await figuresBuild();
  const chunks = output.filter((item) => item.type === 'chunk');
  const ids = chunks.flatMap((chunk) => chunk.moduleIds).map((id) => id.replaceAll('\\', '/'));
  expect(ids.length).toBeGreaterThan(0);
  // Its page and protocol, and the viewer's framing check; nothing from node_modules.
  for (const id of ids)
    expect(id).toMatch(/\/src\/(figures\/[a-z]+\.(ts|css|html)|viewer\/anchor\.ts)$/);
  for (const chunk of chunks) {
    expect(chunk.code).not.toContain('/api/v0');
    expect(chunk.code).not.toContain('sandbox-science-session');
    expect(chunk.code).not.toMatch(/\beval\(|new Function\(/);
  }
  // Every asset is a file of the bundle: no data: script, which the CSP admits nowhere.
  const page = output.find((item) => item.fileName === 'index.html').source;
  expect(page).not.toContain('data:');
  expect(page).toMatch(/<script type="module" crossorigin src="\/figures\/assets\/[\w-]+\.js">/);
}, 60000);
