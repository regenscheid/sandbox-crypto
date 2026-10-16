// SPDX-License-Identifier: MIT
// The figure frame's bundle (docs/specs/figures.md, FG-14): its own code only, none of the
// workspace's client; the chart libraries at their pinned versions, with their notices;
// and every asset a file of its own. That no code is generated is chart.test.ts's.
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

test('the figure frame holds its own code and the chart libraries, with their notices', async () => {
  const output = await figuresBuild();
  const chunks = output.filter((item) => item.type === 'chunk');
  const ids = chunks.flatMap((chunk) => chunk.moduleIds).map((id) => id.replaceAll('\\', '/'));
  const own = ids.filter((id) => !id.includes('/node_modules/') && !id.includes('\0'));
  // Its page, protocol, chart rules, sanitizer, and the viewer's framing check.
  for (const id of own)
    expect(id).toMatch(/\/src\/(figures\/[a-zA-Z]+\.(ts|css|html)|viewer\/anchor\.ts)$/);
  // Vega's, Vega-Lite's and the interpreter's code, at the pinned versions, with notices.
  const report = JSON.parse(output.find((item) => item.fileName === 'license-manifest.json').source);
  const versions = Object.fromEntries(report.packages.map((item) => [item.name, item.version]));
  expect(versions).toMatchObject({ vega: '6.4.0', 'vega-lite': '6.4.3', 'vega-interpreter': '2.3.2' });
  expect(output.find((item) => item.fileName === 'THIRD-PARTY-NOTICES.txt')).toBeDefined();
  for (const chunk of chunks) {
    expect(chunk.code).not.toContain('/api/v0');
    expect(chunk.code).not.toContain('sandbox-science-session');
  }
  // Every asset is a file of the bundle: no data: script, which the CSP admits nowhere.
  const page = output.find((item) => item.fileName === 'index.html').source;
  expect(page).not.toContain('data:');
  expect(page).toMatch(/<script type="module" crossorigin src="\/figures\/assets\/[\w-]+\.js">/);
}, 60000);

test("the figure checker holds the frame's chart code and libraries, with its launcher", async () => {
  const result = await build({
    logLevel: 'silent',
    configFile: resolve(root, 'vite.figurecheck.config.ts'),
    build: { write: false },
  });
  const output = Array.isArray(result) ? result.flatMap((r) => r.output) : result.output;
  expect(output.map((item) => item.fileName).sort()).toEqual([
    'LICENSE-Sandbox-Science.txt',
    'THIRD-PARTY-NOTICES.txt',
    'check-figures.mjs',
    'figurecheck.mjs',
    'license-manifest.json',
  ]);
  const ids = output.flatMap((item) => (item.type === 'chunk' ? item.moduleIds : []));
  const own = ids.map((id) => id.replaceAll('\\', '/')).filter((id) => !id.includes('/node_modules/'));
  // The same rules and run as the frame's (FG-20): no page, worker or sanitizer.
  for (const id of own.filter((id) => !id.includes('\0')))
    expect(id).toMatch(/\/src\/figures\/(figurecheck|vegaChart|chart|protocol)\.ts$/);
  const report = JSON.parse(output.find((item) => item.fileName === 'license-manifest.json').source);
  expect(report.distribution).toBe('figure-checker');
  // It reaches no built-in module of its own: no import, no `require`, no `import()` and
  // no `process`, which its launcher removes (review of #514, L).
  const code = output.find((item) => item.fileName === 'figurecheck.mjs').code;
  const statements = code.replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, '');
  expect(statements).not.toMatch(/^\s*import[\s{*'"]/m);
  expect(statements).not.toMatch(/\bimport\s*\(|\bimport\.meta\b|\brequire\s*\(/);
  expect(statements).not.toMatch(/\bprocess\s*[.[]|globalThis|\bgetBuiltinModule\b|\bnode:/);
  const versions = Object.fromEntries(report.packages.map((item) => [item.name, item.version]));
  expect(versions).toMatchObject({ vega: '6.4.0', 'vega-lite': '6.4.3', 'vega-interpreter': '2.3.2' });
}, 60000);
