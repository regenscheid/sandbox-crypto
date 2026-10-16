// SPDX-License-Identifier: MIT
// Charts (FG-4, FG-15, FG-17): a chart reads only its pinned files or a sequence, names
// no other address, image or link, and Vega-Lite 6 only; it is drawn with no code
// generated, as the frame's CSP requires; and the limits hold.
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  chartLoader,
  checkedChart,
  delimited,
  MAX_ROWS,
  MAX_SEQUENCE,
  Refusal,
  rows,
} from './chart';
import { chartSvg } from './vegaChart';

afterEach(() => vi.restoreAllMocks());

const CSV = 'size,ms\n1024,3.5\n2048,"7,1"\n4096,15\n';
const files = new Map([['data/t.csv', 'cap-t']]);
const read = async (capability: string) => (capability === 'cap-t' ? CSV : '[]');

function refused(source: string, paths: string[] = ['data/t.csv']): string | null {
  try {
    checkedChart(source, paths);
    return null;
  } catch (error) {
    return error instanceof Refusal ? error.code : 'thrown';
  }
}

const bars = (extra: object = {}) =>
  JSON.stringify({
    $schema: 'https://vega.github.io/schema/vega-lite/v6.json',
    title: 'Timing by key size',
    data: { url: 'data/t.csv' },
    mark: 'bar',
    encoding: {
      x: { field: 'size', type: 'ordinal' },
      y: { field: 'ms', type: 'quantitative' },
    },
    ...extra,
  });

describe("a chart's rules", () => {
  it('takes pinned data and a bounded sequence', () => {
    expect(refused(bars())).toBeNull();
    const sequence = { data: { sequence: { start: 0, stop: MAX_SEQUENCE, as: 'x' } } };
    expect(refused(bars(sequence), [])).toBeNull();
  });

  it('refuses inline data, other addresses, images, links and other versions', () => {
    expect(refused(bars({ data: { values: [{ a: 1 }] } }))).toBe('inline_data');
    expect(refused(bars({ datasets: { d: [] } }))).toBe('inline_data');
    expect(refused(bars({ data: { graticule: true } }))).toBe('inline_data');
    expect(refused(bars({ data: { sequence: { start: 0, stop: MAX_SEQUENCE + 1 } } }))).toBe('size');
    expect(refused(bars({ data: { url: 'https://evil.example/d.csv' } }))).toBe('address');
    expect(refused(bars({ data: { url: 'data/other.csv' } }))).toBe('address');
    expect(refused(bars({ layer: [{ data: { url: 'shared/x.json' }, mark: 'point' }] }))).toBe('address');
    expect(refused(bars({ mark: 'image' }))).toBe('address');
    expect(refused(bars({ mark: { type: 'image' } }))).toBe('address');
    expect(refused(bars({ encoding: { href: { field: 'u' } } }))).toBe('address');
    expect(refused(bars({ encoding: { url: { field: 'u' } } }))).toBe('address');
    expect(refused(bars({ $schema: 'https://vega.github.io/schema/vega-lite/v5.json' }))).toBe('version');
    expect(refused('{"mark": ')).toBe('invalid');
    expect(refused('[1]')).toBe('invalid');
    expect(refused(' '.repeat(70_000) + '{}')).toBe('size');
  }); // prettier-ignore
});

describe("a chart's data", () => {
  it('are read as rows by header, quoted fields kept whole', () => {
    expect(delimited(CSV, ',')).toEqual([
      { size: '1024', ms: '3.5' },
      { size: '2048', ms: '7,1' },
      { size: '4096', ms: '15' },
    ]);
    expect(delimited('a\tb\r\n"x ""q"""\t2\r\n', '\t')).toEqual([{ a: 'x "q"', b: '2' }]);
    expect(rows(CSV, 'data/t.csv')).toBe(3);
    expect(rows('[1,2]', 'data/t.json')).toBe(2);
  });

  it('come only from the pinned files, within the limits', async () => {
    const loader = chartLoader(files, read);
    await expect(loader.load('data/t.csv', { context: 'dataflow' })).resolves.toBe(CSV);
    await expect(loader.load('data/other.csv', { context: 'dataflow' })).rejects.toThrow('address');
    await expect(loader.sanitize('data/t.csv', { context: 'href' })).rejects.toThrow('address');
    await expect(loader.sanitize('data/t.csv', { context: 'image' })).rejects.toThrow('address');
    await expect(loader.http()).rejects.toThrow('address');
    const big = 'a\n' + '1\n'.repeat(MAX_ROWS + 1);
    const many = chartLoader(files, async () => big);
    await expect(many.load('data/t.csv', { context: 'dataflow' })).rejects.toThrow('size');
  });
});

describe('drawing a chart', () => {
  it('draws SVG from the pinned data with no code generated, named by its title', async () => {
    // The frame's CSP has no 'unsafe-eval': any code generated would fail there.
    const made = vi.spyOn(globalThis, 'Function').mockImplementation(() => {
      throw new Error('code was generated');
    });
    const { svg, name } = await chartSvg(bars(), files, read);
    expect(made).not.toHaveBeenCalled();
    expect(name).toBe('Timing by key size');
    expect(svg).toMatch(/^<svg/);
    expect((svg.match(/role="graphics-symbol"/g) ?? []).length).toBeGreaterThanOrEqual(3);
  });

  it('refuses a chart that does not compile, or reads a file it does not pin', async () => {
    await expect(chartSvg('{"mark": "nonsense"}', files, read)).rejects.toThrow('invalid');
    await expect(chartSvg(bars(), new Map(), read)).rejects.toThrow('address');
  });
});
