// SPDX-License-Identifier: MIT
/** Whole documents, not isolated table tokens: compare the actual pinned parsers. */
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { expect, it } from 'vitest';
import { attachIds } from '../src/lib/attach';
import { blockMap } from '../src/lib/blockmap';
import { documentParser } from '../src/lib/markdown';
import tables from '../src/lib/tables.fixtures.json';

function corpus() {
  const sources = tables.cases.map(({ markdown }) => markdown);
  const whitespace = ['\ufeff', '\x1c', '\x1d', '\x1e', '\x1f', '\x85', '\xa0', ' ', '\t', ''];
  let seed = 0x174d;
  const choose = (items) => {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    return items[seed % items.length];
  };
  for (let i = 0; i < 1024; i += 1) {
    const edge = choose(whitespace);
    const cell = choose(whitespace);
    const lead = choose(['', '> ', '  ']);
    const header = `${edge}| ${cell}$h_{${i}}$${cell} | right |${edge}`;
    const align = choose(['| --- | --- |', '| :--- | ---: |', '| ---\x85 | --- |']);
    const body = choose([
      `${edge}| ${cell}$b_{${i}}$${cell} | value |${edge}`,
      `| $b_{${i}}$ |`,
      `| $b_{${i}}$ | value | ignored |`,
      `| ${cell}$b_{${i}}$\\${cell} | value |`,
      edge,
    ]);
    let table = [header, align, body].map((line) => lead + line).join('\n');
    if (choose([false, true])) {
      table = '- ' + table.replaceAll('\n', '\n  ');
    }
    const wrapper = choose(['', '::: lemma\n']);
    const separation = choose(['\n', '\n\n']);
    sources.push(
      `# Case ${i} 😀\n\nBefore $p_{${i}}$ and \\(q_{${i}}\\).` + separation +
        wrapper + table + (wrapper ? '\n:::' : '') +
        `\n\n\`\`\`text\n| $code$ |\n\`\`\`\n\nAfter $$a_{${i}}$$.\n`,
    );
  }
  return sources;
}

it('matches full blocks, math, source extents and attached IDs in a seeded Unicode corpus', () => {
  const sources = corpus();
  const root = fileURLToPath(new URL('../../../', import.meta.url));
  const expected = JSON.parse(
    execFileSync('uv', ['run', '--locked', 'python', 'scripts/document_parser_parity.py'], {
      cwd: root,
      input: JSON.stringify(sources),
      encoding: 'utf8',
      maxBuffer: 32 * 1024 * 1024,
      timeout: 30000,
    }),
  );
  sources.forEach((source, index) => {
    const browser = blockMap(source, documentParser.parse(source, {}));
    const server = expected[index];
    const points = (offset) => Array.from(source.slice(0, offset)).length;
    const starts = [0];
    for (let at = 0; at < source.length; at += 1) if (source[at] === '\n') starts.push(at + 1);
    const end = (line) => line + 1 < starts.length ? starts[line + 1] - 1 : source.length;
    const blocks = browser.blocks.map((block) => {
      const place = block.token.meta.place;
      const firsts = place.firsts.slice(0, block.lines[1] - block.lines[0]);
      return {
        id: server.blocks[block.index]?.id,
        kind: block.kind,
        parent: block.parent,
        depth: block.depth,
        lines: block.lines,
        start: points(firsts[0]),
        end: points(end(block.lines[1] - 1)),
        firsts: firsts.map(points),
        text: firsts.map((first, row) => source.slice(first, end(block.lines[0] + row))).join('\n'),
      };
    });
    expect(blocks, `case ${index}: ${JSON.stringify(source)}`).toEqual(server.blocks);
    expect(browser.spans.map(({ block, kind, tex }) => ({ block, kind, tex }))).toEqual(
      server.spans.map(({ block, kind, tex }) => ({ block, kind, tex })),
    );
    // The generated formula bodies are unique within each document. Locate the
    // actual browser token's delimited source independently, including escaped pipes.
    // Fixed fixtures with repeated formula text use their separately pinned positions.
    if (index >= tables.cases.length) {
      browser.spans.forEach((span, at) => {
        const saved = server.spans[at];
        const open = span.token.markup;
        const close = typeof span.token.meta?.close === 'string' ? span.token.meta.close : open;
        expect([saved.open, saved.close]).toEqual([open, close]);
        const raw = `${open}${span.tex}${close}`;
        const begin = source.indexOf(raw);
        expect(begin).toBeGreaterThanOrEqual(0);
        expect(source.indexOf(raw, begin + 1)).toBe(-1);
        expect(saved.positions).toEqual([
          points(begin), points(begin + open.length),
          points(begin + open.length + span.tex.length), points(begin + raw.length),
        ]);
      });
    }
    const attached = attachIds(browser, server.blocks.map((block, at) => ({
      ...block,
      status: 'added',
      math: server.spans.filter((span) => span.block === at),
    })));
    expect(attached.mismatches).toEqual([]);
    expect([...attached.spans.values()]).toEqual(server.spans.map(({ id }) => id));
  });
}, 30000);
