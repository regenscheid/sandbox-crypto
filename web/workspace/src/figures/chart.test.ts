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
  checkDrawn,
  checkSvg,
  MAX_EXPRESSION,
  MAX_FIELD,
  MAX_RUN_CHARACTERS,
  MAX_RUN_ELEMENTS,
  Spending,
  checkBuild,
  formatWidth,
  MAX_SVG,
  MAX_TEXT,
  sceneSize,
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

describe("a chart's memory (review of #500)", () => {
  const texts = (pad: number) =>
    JSON.stringify({
      data: { sequence: { start: 0, stop: MAX_SEQUENCE, as: 'i' } },
      transform: [{ calculate: `pad('', ${pad})`, as: 'label' }],
      mark: 'text',
      encoding: { y: { field: 'i', type: 'quantitative' }, text: { field: 'label' } },
    });

  it('refuses text past its limit before drawing its SVG', async () => {
    // Within every other limit: 10,000 rows, each a label of 200 characters, 2 MB of text.
    await expect(chartSvg(texts(200), new Map(), read)).rejects.toThrow('size');
    const { svg } = await chartSvg(texts(20), new Map(), read);
    expect(svg.length).toBeLessThanOrEqual(MAX_SVG);
  });

  it('refuses a dataset that grows past the row limit in its transforms', async () => {
    const one = JSON.stringify([{ many: Array.from({ length: MAX_ROWS + 1 }, (_, i) => i) }]);
    const flattened = JSON.stringify({
      data: { url: 'data/one.json' },
      transform: [{ flatten: ['many'] }],
      mark: 'point',
      // One mark: the rows after the flatten, not the marks, pass the limit.
      encoding: { y: { aggregate: 'count', type: 'quantitative' } },
    });
    await expect(
      chartSvg(flattened, new Map([['data/one.json', 'cap']]), async () => one),
    ).rejects.toThrow('size');
  });

  it('refuses an impute whose keys would pass the row limit', async () => {
    const imputed = bars({
      transform: [{ impute: 'ms', key: 'size', keyvals: { start: 0, stop: 2_000_000 } }],
    });
    await expect(chartSvg(imputed, files, read)).rejects.toThrow('size');
  });

  it('caps the SVG and the scene, as the worker and the page check them', () => {
    expect(() => checkSvg('x'.repeat(MAX_SVG + 1))).toThrow('size');
    expect(() => checkSvg(5)).toThrow('size');
    expect(checkSvg('<svg/>')).toBe('<svg/>');
    const scene = { items: [{ items: [{ text: 'ab' }, { text: ['c', 'de'] }, { text: 7 }] }] };
    expect(sceneSize(scene)).toEqual({ marks: 4, text: 6 });
    expect(() => checkDrawn({ items: [{ text: 'x'.repeat(MAX_TEXT + 1) }] }, [])).toThrow('size');
    expect(() => checkDrawn(scene, [MAX_ROWS + 1])).toThrow('size');
    expect(() => checkDrawn(scene, [MAX_ROWS])).not.toThrow();
  });
});

describe("what a chart's expressions make (review of #500, memory)", () => {
  const rows = (stop: number) => ({ sequence: { start: 0, stop, as: 'i' } });

  it('refuses many long strings grouped, with no data file and no text mark', async () => {
    // The first crash: pad() of 20,000 on each of 10,000 rows, then a groupby.
    const grouped = JSON.stringify({
      data: rows(10_000),
      transform: [
        { calculate: "pad('' + datum.i, 20000)", as: 'p' },
        { aggregate: [{ op: 'count', as: 'n' }], groupby: ['p'] },
      ],
      mark: 'bar',
      encoding: { x: { field: 'p', type: 'nominal' }, y: { field: 'n', type: 'quantitative' } },
    });
    expect(grouped.length).toBeLessThan(2000);
    await expect(chartSvg(grouped, new Map(), read)).rejects.toThrow('size');
  });

  it('refuses a string doubled past its limit by concatenation', async () => {
    // The second crash: 28 doublings, then upper(), on 8 rows.
    const doubling = Array.from({ length: 28 }, () => ({ calculate: 'datum.s + datum.s', as: 's' }));
    const doubled = JSON.stringify({
      data: rows(8),
      transform: [{ calculate: "'abcdefgh'", as: 's' }, ...doubling, { calculate: 'upper(datum.s)', as: 'u' }],
      mark: 'point',
      encoding: { x: { field: 'i', type: 'quantitative' } },
    });
    await expect(chartSvg(doubled, new Map(), read)).rejects.toThrow('size');
  });

  it('refuses sequences made row by row past the run’s elements', async () => {
    const sequences = JSON.stringify({
      data: rows(1_000),
      transform: [{ calculate: 'sequence(0, 5000)', as: 'many' }],
      mark: 'point',
      encoding: { x: { field: 'i', type: 'quantitative' } },
    });
    await expect(chartSvg(sequences, new Map(), read)).rejects.toThrow('size');
  });

  it('refuses an expression whose text passes 1 KiB', () => {
    const long = '1' + ' + 1'.repeat(MAX_EXPRESSION);
    expect(() => checkedChart(bars({ transform: [{ calculate: long, as: 'x' }] }), ['data/t.csv'])).toThrow('size');
    expect(() => checkedChart(bars({ transform: [{ filter: long }] }), ['data/t.csv'])).toThrow('size');
  });

  it('spends what values hold, and checks what transforms make', () => {
    const spending = new Spending();
    expect(spending.spend('abc')).toBe('abc');
    expect(() => spending.spend('x'.repeat(MAX_FIELD + 1))).toThrow('size');
    expect(() => new Spending().spend(Array(MAX_RUN_ELEMENTS + 1).fill(0))).toThrow('size');
    const many = new Spending();
    expect(() => {
      for (let i = 0; i < MAX_RUN_CHARACTERS / MAX_FIELD + 1; i++) many.spend('x'.repeat(MAX_FIELD));
    }).toThrow('size');
    expect(() => new Spending().rows([{ s: 'x'.repeat(MAX_FIELD + 1) }])).toThrow('size');
    const arrays = [{ a: Array(MAX_RUN_ELEMENTS / 2 + 1).fill(0) }];
    expect(() => new Spending().rows(arrays, arrays)).toThrow('size');
    expect(() => new Spending().rows([{ a: [1, 2], s: 'ok' }])).not.toThrow();
  });

  it('still draws an ordinary chart with labels and a formula', async () => {
    const labelled = bars({
      transform: [{ calculate: "upper('ms ') + format(datum.ms, '.1f')", as: 'label' }],
      mark: 'bar',
    });
    const { svg } = await chartSvg(labelled, files, read);
    expect(svg).toMatch(/^<svg/);
  });
}); // prettier-ignore

describe("what a chart's functions read and build (review of #500, L1 and L2)", () => {
  const rows = (stop: number) => ({ sequence: { start: 0, stop, as: 'i' } });
  const chart = (transform: object[], stop = 8) =>
    JSON.stringify({
      data: rows(stop),
      transform,
      mark: 'point',
      encoding: { x: { field: 'i', type: 'quantitative' } },
    });

  it('refuses a string doubled inside an object, then read by indexof', async () => {
    // L1's crash: {v: datum.o.v + datum.o.v} 28 times, then indexof(), on 8 rows.
    const doubling = Array.from({ length: 28 }, () => ({ calculate: '{v: datum.o.v + datum.o.v}', as: 'o' }));
    const nested = chart([{ calculate: "{v: 'abcdefgh'}", as: 'o' }, ...doubling, { calculate: "indexof(datum.o.v, 'z')", as: 'n' }]);
    await expect(chartSvg(nested, new Map(), read)).rejects.toThrow('size');
    const listed = chart([{ calculate: "['abcdefgh']", as: 'o' }, ...Array.from({ length: 28 }, () => ({ calculate: '[datum.o[0] + datum.o[0]]', as: 'o' }))]);
    await expect(chartSvg(listed, new Map(), read)).rejects.toThrow('size');
  });

  it('refuses a long string read by any function, before it is read', async () => {
    const long = { calculate: "pad('', 40000)", as: 's' };
    for (const read_ of ["indexof(datum.s + datum.s, 'z')", "lastindexof(datum.s + datum.s, 'z')", "test('z', datum.s + datum.s)", 'parseFloat(datum.s + datum.s)', 'parseInt(datum.s + datum.s)', 'length(datum.s + datum.s)', 'toNumber(datum.s + datum.s)'])
      await expect(chartSvg(chart([long, { calculate: read_, as: 'n' }]), new Map(), read), read_).rejects.toThrow('size');
  });

  it('refuses what a function would build past the limits, before building it', async () => {
    // L2's crashes: pad() to 1e8, and join() of 8,000 elements with a 60 Ki separator.
    for (const build of ["pad('', 1e8)", "join(sequence(0, 8000), pad('', 60000))", "replace(pad('', 1000, 'a'), regexp('a', 'g'), pad('', 1000))", "replace(pad('', 1000, 'a'), regexp('a', 'g'), \"$'\")", "truncate(pad('', 60000), 60000, 'right', pad('', 60000))", "format(1, '100000d')", "timeSequence('milliseconds', 0, 1e9)", "btoa(pad('', 60000))"]) {
      const started = performance.now();
      await expect(chartSvg(chart([{ calculate: build, as: 'b' }], 1), new Map(), read), build).rejects.toThrow('size');
      expect(performance.now() - started, build).toBeLessThan(2000);
    }
    expect(() => checkedChart(bars({ encoding: { y: { field: 'ms', type: 'quantitative', axis: { format: '100000d' } } } }), ['data/t.csv'])).toThrow('size');
  });

  it('estimates what a function builds from its arguments', () => {
    const fits = (name: string, ...args: unknown[]) => { try { checkBuild(name, args); return true; } catch { return false; } };
    expect(fits('pad', 'x', MAX_FIELD)).toBe(true);
    expect(fits('pad', 'x', MAX_FIELD + 1)).toBe(false);
    expect(fits('pad', 'x', MAX_FIELD / 2, 'ab')).toBe(true);
    expect(fits('pad', 'x', MAX_FIELD / 2 + 2, 'ab')).toBe(false);
    expect(fits('join', ['a', 'b'], '-')).toBe(true);
    expect(fits('join', [['x'.repeat(MAX_FIELD / 2)], ['x'.repeat(MAX_FIELD / 2)]], 'yy')).toBe(false);
    expect(fits('replace', 'a,b,c', /,/g, ', ')).toBe(true);
    expect(fits('replace', 'a'.repeat(300), /a/g, 'b'.repeat(300))).toBe(false);
    expect(fits('replace', 'a'.repeat(300), /a/, 'b'.repeat(300))).toBe(true);
    expect(fits('replace', 'a'.repeat(300), /x*/g, 'b'.repeat(300))).toBe(false);
    expect(fits('truncate', 'x'.repeat(MAX_FIELD), 10)).toBe(true);
    expect(fits('format', 1, ',.2f')).toBe(true);
    expect(fits('format', 1, `>${MAX_FIELD + 1}d`)).toBe(false);
    expect(formatWidth('010d')).toBe(10);
    expect(formatWidth('.100f')).toBe(0);
    expect(fits('timeSequence', 'day', new Date(2020, 0, 1), new Date(2021, 0, 1))).toBe(true);
    expect(fits('utcSequence', 'seconds', 0, 1e9)).toBe(false);
    expect(fits('utcSequence', 'seconds', 0, 1e9, 1000)).toBe(true);
    expect(fits('indexof', 'x', 'y')).toBe(true);
  });

  it('spends the strings and arrays nested in a value, but not the rows of the data', () => {
    const doubled = { o: { v: 'x'.repeat(MAX_FIELD + 1) } };
    expect(() => new Spending().spend(doubled)).toThrow('size');
    expect(() => new Spending().spend([['x'.repeat(MAX_FIELD + 1)]])).toThrow('size');
    expect(() => new Spending().rows([{ o: doubled }])).toThrow('size');
    expect(() => new Spending().rows([{ a: [{ s: 'x'.repeat(MAX_FIELD + 1) }] }])).toThrow('size');
    const spending = new Spending();
    spending.spend({ a: ['ab', { b: 'cde' }] });
    expect([spending.characters, spending.elements]).toEqual([5, 2]);
    // A row of the run's data is accounted for by its transform: not spent again.
    const row = { s: 'x'.repeat(MAX_FIELD + 1) };
    const rowsOwned = new Spending((value) => value === row);
    expect(() => rowsOwned.spend([row])).not.toThrow();
    expect(() => rowsOwned.rows([{ lookup: row }])).not.toThrow();
    const shared = { s: 'abc' };
    const twice = new Spending();
    twice.spend({ a: shared, b: shared });
    expect(twice.characters).toBe(3);
  });

  it('still draws charts that pad, join, replace, format and step through time', async () => {
    const ordinary = bars({
      transform: [
        { calculate: "pad(format(datum.ms, '.1f'), 6, ' ', 'left')", as: 'padded' },
        { calculate: "join([datum.size, datum.ms], ' / ')", as: 'joined' },
        { calculate: "replace(datum.joined, regexp('/', 'g'), 'per')", as: 'replaced' },
        { calculate: "{label: truncate(datum.replaced, 8), n: length(datum.replaced)}", as: 'o' },
        { calculate: "length(timeSequence('day', datetime(2020, 0, 1), datetime(2020, 1, 1)))", as: 'days' },
        { filter: "indexof(datum.o.label, 'per') >= 0 && test('^[0-9]', datum.padded + '1') || true" },
      ],
      encoding: {
        x: { field: 'size', type: 'ordinal' },
        y: { field: 'ms', type: 'quantitative', axis: { format: '08.2f' } },
        tooltip: { field: 'o.label' },
      },
    });
    const { svg } = await chartSvg(ordinary, files, read);
    expect(svg).toMatch(/^<svg/);
  });
}); // prettier-ignore
