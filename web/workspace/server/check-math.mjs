// SPDX-License-Identifier: MIT
// Trusted launcher for one batch of formulas from one source. Python supplies its hard
// deadline and bounded pipes; each formula gets its own macro definitions.
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

const MAX_INPUT = 262_144;
const MAX_BATCH = 32;
const MAX_ANSWER = 32_768;
const [major, minor] = process.versions.node.split('.').map(Number);
if (major < 22 || (major === 22 && minor < 12)) throw new Error('Node 22.12 or newer required');
let size = 0;
const parts = [];
for await (const chunk of process.stdin) {
  size += chunk.length;
  if (size > MAX_INPUT) throw new Error('math request too large');
  parts.push(chunk);
}
const batch = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(parts)));
if (!Array.isArray(batch) || batch.length < 1 || batch.length > MAX_BATCH) {
  throw new Error('invalid math batch');
}
const ids = new Set();
for (const input of batch) {
  if (input === null || typeof input !== 'object' || Array.isArray(input)
      || Object.keys(input).sort().join(',') !== 'display,id,macros,tex'
      || typeof input.id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(input.id)
      || ids.has(input.id)
      || typeof input.tex !== 'string' || input.tex.length > 20_000
      || typeof input.display !== 'boolean'
      || input.macros === null || typeof input.macros !== 'object' || Array.isArray(input.macros)
      || Object.entries(input.macros).length > 200
      || Object.entries(input.macros).some(([name, value]) => name.length > 100
        || typeof value !== 'string' || value.length > 1_000)) {
    throw new Error('invalid math request');
  }
  ids.add(input.id);
}
// The bundle is operator configuration, never a document or model-supplied path.
if (process.argv.length !== 3) throw new Error('math bundle path required');
const { checkFormula } = await import(pathToFileURL(resolve(process.argv[2])).href);
const answers = batch.map((input) => {
  const answer = JSON.stringify({ ...checkFormula(input), runtime: process.versions.node });
  if (Buffer.byteLength(answer) > MAX_ANSWER) throw new Error('math result too large');
  return answer;
});
process.stdout.write('[' + answers.join(',') + ']\n');
