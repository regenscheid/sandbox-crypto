// SPDX-License-Identifier: MIT
import { createHash } from 'node:crypto';
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, expect, test } from 'vitest';
import { build } from 'vite';
import mathConfig from '../vite.math.config.ts';
import { collectNotices, verifyMathDistribution } from './license-notices.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
const manifest = JSON.parse(readFileSync(resolve(root, 'licenses/manifest.json'), 'utf8'));
const temporary = [];
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
afterEach(() => { for (const path of temporary.splice(0)) rmSync(path, { recursive: true, force: true }); });

function fixture() {
  const temp = mkdtempSync(resolve(tmpdir(), 'math-notices-'));
  temporary.push(temp);
  const app = resolve(temp, 'web/workspace');
  mkdirSync(app, { recursive: true });
  const copy = (from) => {
    mkdirSync(dirname(resolve(app, from)), { recursive: true });
    cpSync(resolve(root, from), resolve(app, from));
  };
  cpSync(resolve(root, 'licenses'), resolve(app, 'licenses'), { recursive: true });
  for (const file of ['../../LICENSE', 'package-lock.json', 'server/check-math.mjs',
    'node_modules/katex/package.json', 'node_modules/katex/LICENSE', 'node_modules/katex/dist/katex.mjs',
    'node_modules/katex/src/utils.ts', 'node_modules/katex/src/stretchy.ts', 'src/lib/mathcheck.ts']) copy(file);
  const bundle = { 'mathcheck.mjs': { type: 'chunk', fileName: 'mathcheck.mjs', code: 'export {};',
    moduleIds: [resolve(app, 'src/lib/mathcheck.ts'), resolve(app, 'node_modules/katex/dist/katex.mjs')],
    imports: [], dynamicImports: [] } };
  const collect = () => collectNotices(app, bundle, 'math');
  const write = () => {
    mkdirSync(resolve(app, 'dist-math'), { recursive: true });
    for (const [file, bytes] of Object.entries({ 'mathcheck.mjs': bundle['mathcheck.mjs'].code, ...collect() })) {
      writeFileSync(resolve(app, 'dist-math', file), bytes);
    }
  };
  return { app, bundle, collect, write };
}

test('actual math builds retain complete code-only notices, unchanged runtime bytes and deterministic coverage', async () => {
  const options = { root, logLevel: 'silent', build: { write: false } };
  const outputs = result => Array.isArray(result) ? result.flatMap(item => item.output) : result.output;
  const first = outputs(await build({ ...options, configFile: resolve(root, 'vite.math.config.ts') }));
  const second = outputs(await build({ ...options, configFile: resolve(root, 'vite.math.config.ts') }));
  const baseline = outputs(await build({ ...mathConfig, configFile: false, plugins: [], ...options,
    build: { ...mathConfig.build, write: false } }));
  const contents = output => new Map(output.map(item => [item.fileName, item.type === 'chunk' ? item.code : item.source]));
  const a = contents(first), b = contents(second);
  expect([...a.keys()].sort()).toEqual(['LICENSE-KaTeX.txt', 'LICENSE-Sandbox-Science.txt',
    'THIRD-PARTY-NOTICES.txt', 'check-math.mjs', 'license-manifest.json', 'mathcheck.mjs'].sort());
  for (const [name, bytes] of a) expect(hash(bytes)).toBe(hash(b.get(name)));
  expect(a.get('mathcheck.mjs')).toBe(contents(baseline).get('mathcheck.mjs'));
  expect(a.get('check-math.mjs')).toBe(readFileSync(resolve(root, 'server/check-math.mjs'), 'utf8'));
  expect(a.get('LICENSE-KaTeX.txt')).toBe(readFileSync(resolve(root, 'node_modules/katex/LICENSE'), 'utf8'));
  expect(a.get('LICENSE-Sandbox-Science.txt')).toBe(readFileSync(resolve(root, '../../LICENSE'), 'utf8'));
  const notice = a.get('THIRD-PARTY-NOTICES.txt');
  expect(notice).toContain('isolated math checker');
  expect(notice).toContain(readFileSync(resolve(root, 'licenses/React-Apache-2.0.txt'), 'utf8'));
  expect(notice).toContain(manifest.katexEmbedded['src/utils.ts'].attribution);
  expect(notice).toContain(manifest.katexEmbedded['src/stretchy.ts'].attribution);
  expect(notice).toContain('SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007');
  expect(notice).not.toContain('then/is-promise');
  expect(notice).not.toContain('<Copyright Holder>');
  const report = JSON.parse(a.get('license-manifest.json'));
  expect(report.packages.map(p => p.name)).toEqual(['katex']);
  expect(report.externalRuntime).toEqual({ name: 'Node.js', providedBy: 'operator', builtins: ['node:path', 'node:url'] });
  expect(report.fontAndSvgLicense.sha256).toBe(manifest.ofl.sha256);
  expect(report.isPromiseLicense).toBeNull();
  expect(report.files.map(f => f.file).sort()).toEqual([...a.keys()].filter(f => f !== 'license-manifest.json').sort());
  for (const file of report.files) expect(hash(a.get(file.file))).toBe(file.sha256);
  for (const item of first.filter(item => item.type === 'chunk')) {
    expect(item.imports).toEqual([]);
    expect(item.dynamicImports).toEqual([]);
  }
}, 10000);

test('math mode leaves default GUI notice generation byte-identical', () => {
  const { app, bundle } = fixture();
  expect(collectNotices(app, bundle)).toEqual(collectNotices(app, bundle, 'gui'));
  expect(collectNotices(app, bundle)['check-math.mjs']).toBeUndefined();
});

test.each(['licenses/React-Apache-2.0.txt', 'licenses/OFL-1.1.txt', 'node_modules/katex/LICENSE',
  'node_modules/katex/src/utils.ts', 'node_modules/katex/src/stretchy.ts', 'server/check-math.mjs'])(
  'math refuses missing or changed required source: %s', file => {
    const { app, collect } = fixture();
    const original = readFileSync(resolve(app, file));
    writeFileSync(resolve(app, file), Buffer.concat([original, Buffer.from('\nchanged')]));
    expect(collect).toThrow();
    rmSync(resolve(app, file));
    expect(collect).toThrow();
  });

test.each(['mathcheck.mjs', 'check-math.mjs', 'LICENSE-KaTeX.txt', 'LICENSE-Sandbox-Science.txt',
  'THIRD-PARTY-NOTICES.txt'])( 'math verification refuses missing or changed distribution bytes: %s', file => {
  const { app, write } = fixture();
  write();
  expect(() => verifyMathDistribution(app)).not.toThrow();
  writeFileSync(resolve(app, 'dist-math', file), 'changed');
  expect(() => verifyMathDistribution(app)).toThrow();
  rmSync(resolve(app, 'dist-math', file));
  expect(() => verifyMathDistribution(app)).toThrow();
});

test('math refuses uncovered files, incomplete manifests, originless chunks and external modules', () => {
  const { app, bundle, collect, write } = fixture();
  write();
  writeFileSync(resolve(app, 'dist-math', 'uncovered.bin'), 'asset');
  expect(() => verifyMathDistribution(app)).toThrow('Uncovered');
  rmSync(resolve(app, 'dist-math', 'uncovered.bin'));
  const report = JSON.parse(readFileSync(resolve(app, 'dist-math/license-manifest.json'), 'utf8'));
  report.files.pop();
  writeFileSync(resolve(app, 'dist-math/license-manifest.json'), JSON.stringify(report));
  expect(() => verifyMathDistribution(app)).toThrow('Incomplete');
  bundle['mathcheck.mjs'].imports.push('node:fs');
  expect(collect).toThrow('Uncovered external');
  bundle['mathcheck.mjs'].imports = [];
  bundle['mathcheck.mjs'].moduleIds = [];
  expect(collect).toThrow('no license origins');
});
