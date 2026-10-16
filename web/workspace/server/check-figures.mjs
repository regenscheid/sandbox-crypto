// SPDX-License-Identifier: MIT
// Trusted launcher for one batch of charts (docs/specs/figures.md, FG-20). Python supplies
// its hard deadline and bounded pipes. Each chart arrives with its pinned data's text; the
// bundle reads no file and reaches no address.
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

const MAX_INPUT = 20 * 1024 * 1024;
const MAX_BATCH = 8;
const MAX_SOURCE = 65_536;
const MAX_DATA = 8 * 1024 * 1024;
const MAX_FILES = 16;
const MAX_ANSWER = 32_768;
const [major, minor] = process.versions.node.split('.').map(Number);
if (major < 22 || (major === 22 && minor < 12)) throw new Error('Node 22.12 or newer required');
let size = 0;
const parts = [];
for await (const chunk of process.stdin) {
  size += chunk.length;
  if (size > MAX_INPUT) throw new Error('figure request too large');
  parts.push(chunk);
}
const batch = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(parts)));
if (!Array.isArray(batch) || batch.length < 1 || batch.length > MAX_BATCH) {
  throw new Error('invalid figure batch');
}
const ids = new Set();
for (const input of batch) {
  const files = input?.files;
  if (input === null || typeof input !== 'object' || Array.isArray(input)
      || Object.keys(input).sort().join(',') !== 'files,id,source'
      || typeof input.id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(input.id)
      || ids.has(input.id)
      || typeof input.source !== 'string' || input.source.length > MAX_SOURCE
      || files === null || typeof files !== 'object' || Array.isArray(files)
      || Object.keys(files).length > MAX_FILES
      || Object.entries(files).some(([path, text]) => path.length > 4096 || typeof text !== 'string')
      || Object.values(files).reduce((sum, text) => sum + Buffer.byteLength(text), 0) > MAX_DATA) {
    throw new Error('invalid figure request');
  }
  ids.add(input.id);
}
// The bundle is operator configuration, never a document or model-supplied path.
if (process.argv.length !== 3) throw new Error('figure bundle path required');
const bundle = pathToFileURL(resolve(process.argv[2])).href;
const { stdout } = process;
const exit = process.exit.bind(process);
const runtime = process.versions.node;
// Node's permission model, which the orchestrator turns on, bounds files, child processes
// and workers but not the network: the bundle gets no client for it (review of #513, M3),
// and no way to the built-in modules that are one, such as node:net and node:http, which
// `process.getBuiltinModule` would give it. The bundle imports nothing and names no
// `process`, `require` or `import()` (figures-bundle.test.mjs), so with the `process`
// global gone no code it runs reaches a built-in module (review of #514, L).
for (const name of ['fetch', 'WebSocket', 'EventSource']) delete globalThis[name];
delete process.getBuiltinModule;
delete globalThis.process;
for (const name of ['fetch', 'WebSocket', 'EventSource', 'process', 'require', 'module'])
  if (name in globalThis) throw new Error(`${name} could not be removed`);
const { checkFigure } = await import(bundle);
const answers = [];
for (const input of batch) {
  const answer = JSON.stringify({ ...(await checkFigure(input)), runtime });
  if (Buffer.byteLength(answer) > MAX_ANSWER) throw new Error('figure result too large');
  answers.push(answer);
}
stdout.write('[' + answers.join(',') + ']\n', () => exit(0));
