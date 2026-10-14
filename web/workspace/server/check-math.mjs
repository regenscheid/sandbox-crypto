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
// KaTeX's parser recurses once per level of nesting, and how deep it gets before the
// stack runs out depends on how warm V8 is, so on what ran before in the process. A
// formula nested deeper than this is unavailable, whatever its batch; a cold process
// manages more than three times as deep.
const MAX_DEPTH = 150;
function depth(tex) {
  let level = 0;
  let deepest = 0;
  for (let i = 0; i < tex.length; i += 1) {
    if (tex[i] === '\\') {
      const word = /^\\(left|right)(?![A-Za-z])/.exec(tex.slice(i, i + 7));
      if (word) level += word[1] === 'left' ? 1 : -1;
      i += word ? word[0].length - 1 : 1; // an escaped brace is not a group
    } else if (tex[i] === '{') level += 1;
    else if (tex[i] === '}') level -= 1;
    deepest = Math.max(deepest, level);
  }
  return deepest;
}
function nesting(input) {
  // A macro adds its own nesting where it is used; count the deepest one once.
  const macros = Object.values(input.macros).map(depth);
  return depth(input.tex) + Math.max(0, ...macros);
}
// The bundle is operator configuration, never a document or model-supplied path.
if (process.argv.length !== 3) throw new Error('math bundle path required');
const { checkFormula } = await import(pathToFileURL(resolve(process.argv[2])).href);
function check(input) {
  if (nesting(input) <= MAX_DEPTH) return checkFormula(input);
  const common = checkFormula({ id: input.id, tex: '', display: input.display, macros: {} });
  return { ...common, status: 'unavailable', error: 'formula nests too deeply', remedy: null };
}
const answers = batch.map((input) => {
  const answer = JSON.stringify({ ...check(input), runtime: process.versions.node });
  if (Buffer.byteLength(answer) > MAX_ANSWER) throw new Error('math result too large');
  return answer;
});
process.stdout.write('[' + answers.join(',') + ']\n');
