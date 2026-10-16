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
  Refusals,
  boundedSequence,
  drawnInWorker,
  jsonRows,
  type ChartWorker,
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
  });

  it('are bounded in rows by the readers themselves, whatever ends a line (review of #500, L1)', () => {
    for (const end of ['\n', '\r', '\r\n'])
      expect(() => delimited('a' + end + ('1' + end).repeat(MAX_ROWS + 1), ',')).toThrow('size');
    expect(delimited('a\r' + '1\r'.repeat(MAX_ROWS), ',')).toHaveLength(MAX_ROWS);
    const nested = JSON.stringify({ d: { rows: Array(MAX_ROWS + 1).fill(1) } });
    expect(() => jsonRows(nested, 'd.rows')).toThrow('size');
    expect(jsonRows('{"d": {"rows": [1, 2]}}', 'd.rows')).toEqual([1, 2]);
    expect(() => boundedSequence(0, 2e6)).toThrow('size');
    expect(boundedSequence(3)).toEqual([0, 1, 2]);
  });

  it('come only from the pinned files, within the limits', async () => {
    const loader = chartLoader(files, read, new Refusals());
    await expect(loader.load('data/t.csv', { context: 'dataflow' })).resolves.toBe(CSV);
    await expect(loader.load('data/other.csv', { context: 'dataflow' })).rejects.toThrow('address');
    await expect(loader.sanitize('data/t.csv', { context: 'href' })).rejects.toThrow('address');
    await expect(loader.sanitize('data/t.csv', { context: 'image' })).rejects.toThrow('address');
    await expect(loader.http()).rejects.toThrow('address');
    // Each refusal is noted, to refuse the chart after Vega's run (review of #500, M2).
    const refusals = new Refusals();
    const failing = chartLoader(files, () => Promise.reject(new Error('gone')), refusals);
    await expect(failing.load('data/t.csv', { context: 'dataflow' })).rejects.toThrow('refused');
    expect(refusals.first?.code).toBe('refused');
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
    const topo = bars({ data: { url: 'data/t.csv', format: { type: 'topojson' } } });
    await expect(chartSvg(topo, files, read)).rejects.toThrow('invalid');
  });

  it('refuses, never draws without, data Vega could not read (review of #500, M2)', async () => {
    const huge = 'size,ms\n' + '1,2\n'.repeat(200_000);
    await expect(chartSvg(bars(), files, async () => huge)).rejects.toThrow('size');
    const expired = async () => Promise.reject(new Refusal('refused'));
    await expect(chartSvg(bars(), files, expired)).rejects.toThrow('refused');
    const broken = async () => Promise.reject(new TypeError('network'));
    await expect(chartSvg(bars(), files, broken)).rejects.toThrow('refused');
    // A layer whose data cannot be read is no silent omission.
    const layered = bars({
      layer: [{ mark: 'bar' }, { data: { url: 'data/t.csv' }, mark: 'rule' }],
    });
    await expect(chartSvg(layered, files, async () => huge)).rejects.toThrow('size');
  });

  it('bounds a sequence an expression makes, as a data sequence is (review of #500, L2)', async () => {
    const calculated = bars({
      transform: [{ calculate: 'sequence(0, 2000000)', as: 'many' }, { flatten: ['many'] }],
    });
    await expect(chartSvg(calculated, files, read)).rejects.toThrow('size');
  });
});

describe("a chart's worker (review of #500, M1)", () => {
  function fakeWorker(answer?: unknown): ChartWorker & { ended: number; sent: unknown[] } {
    const worker = {
      ended: 0,
      sent: [] as unknown[],
      onmessage: null as ChartWorker['onmessage'],
      onerror: null as ChartWorker['onerror'],
      postMessage(message: unknown) {
        worker.sent.push(message);
        if (answer !== undefined) queueMicrotask(() => worker.onmessage?.({ data: answer }));
      },
      terminate() {
        worker.ended += 1;
      },
    };
    return worker;
  }
  const message = { source: bars(), paths: ['data/t.csv'], capabilities: ['c'] };

  it('is terminated at the time limit, and the chart is past its limit (FG-17)', async () => {
    vi.useFakeTimers();
    try {
      const runaway = fakeWorker(); // never answers, as a chart that runs on
      const drawn = drawnInWorker(runaway, message, 4000);
      const settled = expect(drawn).rejects.toThrow('time');
      vi.advanceTimersByTime(3999);
      expect(runaway.ended).toBe(0);
      vi.advanceTimersByTime(1);
      await settled;
      expect(runaway.ended).toBe(1);
      expect(runaway.sent).toEqual([message]);
    } finally {
      vi.useRealTimers();
    }
  });

  it('gives the SVG it drew, or the code it refused with, and is ended either way', async () => {
    const good = fakeWorker({ ok: true, svg: '<svg/>', name: 'T' });
    await expect(drawnInWorker(good, message)).resolves.toEqual({ svg: '<svg/>', name: 'T' });
    expect(good.ended).toBe(1);
    const refused = fakeWorker({ ok: false, code: 'inline_data' });
    await expect(drawnInWorker(refused, message)).rejects.toThrow('inline_data');
    const odd = fakeWorker({ ok: false, code: '<b>x</b>' });
    await expect(drawnInWorker(odd, message)).rejects.toThrow('unreadable');
    expect(odd.ended).toBe(1);
  });
});
