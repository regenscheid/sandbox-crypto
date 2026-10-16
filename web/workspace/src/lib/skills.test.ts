// SPDX-License-Identifier: MIT
// The skills page and a run's skills (SK-24): an import's report read into its parts,
// with what the pass derived marked; a run's discoveries with how they matched and its
// loads and reads; the user's overrides; and the views render pack text as text, hidden
// characters as their code points.
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import RunSkills from '../views/RunSkills.svelte';
import SkillPinCard from '../views/SkillPinCard.svelte';
import SkillTable from '../views/SkillTable.svelte';
import type { RunRecord } from './api';
import type { SkillPinView, SkillView } from './api-types';
import { hostileMarkup } from './approvalTesting';
import { parseRoute, skillsHref } from './router';
import { idLines, overridesView, reportView, runSkills, scoringText } from './skills';

const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';

const report = {
  skills: ['alpha', 'beta'],
  hidden_characters: [{ path: 'alpha/SKILL.md', characters: 3 }],
  broken_links: [{ path: 'beta/SKILL.md', link: HOSTILE }],
  warnings: ['beta: a long description‮'],
  skipped: [{ path: 'x.bin', reason: 'binary' }],
  left_out: { p1: ['plan'], p2: ['other'] },
  derived: {
    alias: 'fast',
    model: 'small-1',
    cached: true,
    topics: { [HOSTILE]: ['alpha'] },
    entry: ['alpha'],
    descriptions: { beta: 'Counts​ lines.' },
    bindings: { DATA: 'data/' },
    held: [{ variable: 'DATA', path: 'data/', reason: 'not namespaced‮' }],
    dropped: [{ skill: 'gamma' }],
  },
};

const pin: SkillPinView = {
  source: 'ss',
  revision: 2,
  skills: ['alpha', 'beta'],
  left_out: [],
  user_set: null,
  pinned: {
    import_id: 4,
    source: 'ss',
    kind: 'git',
    origin: 'https://example.test/pack.git',
    ref: 'main',
    commit: '0123456789abcdef0123',
    archive_sha256: null,
    created: '2026-10-01T00:00:00+00:00',
    report,
  },
};

describe('reportView', () => {
  it('reads the report, marking what the pass derived', () => {
    const view = reportView(report, 'p1');
    expect(view.skills).toEqual(['alpha', 'beta']);
    expect(view.hidden).toEqual([{ path: 'alpha/SKILL.md', count: 3 }]);
    expect(view.broken).toEqual([{ path: 'beta/SKILL.md', link: HOSTILE }]);
    expect(view.leftOut).toEqual(['plan']);
    expect(view.skipped).toEqual([{ path: 'x.bin', reason: 'binary' }]);
    expect(view.warnings[0]).toContain('U+202E');
    expect(view.derived?.by).toBe('small-1 (alias fast), cached');
    expect(view.derived?.descriptions).toEqual([['beta', 'Counts⟦U+200B⟧ lines.']]);
    expect(view.derived?.dropped).toBe(1);
    expect(view.derived?.held).toEqual([
      { variable: 'DATA', path: 'data/', reason: 'not namespaced⟦U+202E⟧' },
    ]);
  });

  it('takes a report without a pass or with odd fields', () => {
    const view = reportView({ hidden_characters: [{ path: 1, characters: 'many' }] }, 'p1');
    expect(view.derived).toBeNull();
    expect(view.hidden).toEqual([{ path: '?', count: 0 }]);
    expect(view.leftOut).toEqual([]);
  });
});

describe('a run’s skills', () => {
  const run: Pick<RunRecord, 'tools' | 'summaries' | 'delivered'> = {
    summaries: {
      skills: {
        discoveries: [
          {
            answered: 'task',
            scoring: 'hybrid',
            skills: [{ id: 'ss/alpha', revision: 'r1' }],
            entry: [{ id: 'ss/beta', revision: 'r1' }],
            notice: 'shown-1',
          },
          { answered: 'message', scoring: 'timed_out', skills: [] },
          { answered: 'message', scoring: 'lexical', skills: [{ id: HOSTILE }], notice: 'shown-3' },
        ],
        loads: [{ trigger: 'start', id: 'builtin/plan', revision: 'r', status: 'loaded' }],
      },
    },
    delivered: { skills: ['shown-1'] },
    tools: [
      {
        tool: 'skills',
        arguments: '{}',
        skills: { action: 'load', loads: [{ id: 'ss/alpha', status: 'loaded' }] },
      },
      { tool: 'skills', arguments: '{}', skills: { action: 'read', id: 'ss/alpha', path: 'a.md' } },
      {
        tool: 'skills',
        arguments: '{}',
        skills: { action: 'search', scoring: 'lexical', found: [], status: 'ok' },
      },
      { tool: 'shell', arguments: '{}' },
    ],
  };

  it('reads discoveries, with how they matched and whether a call saw them', () => {
    const { discoveries, uses } = runSkills(run);
    expect(discoveries.map((d) => [d.answered, d.scoring, d.skills, d.seen])).toEqual([
      ['task', 'by words and meaning', ['ss/alpha'], true],
      ['message', 'not compared: it timed out', [], null],
      ['message', 'by words', [HOSTILE], false],
    ]);
    expect(discoveries[0]!.entry).toEqual(['ss/beta']);
    expect(uses).toEqual([
      { how: 'loaded at the start', skill: 'builtin/plan', detail: 'loaded' },
      { how: 'loaded', skill: 'ss/alpha', detail: 'loaded' },
      { how: 'read', skill: 'ss/alpha', detail: 'a.md' },
      { how: 'searched, by words', skill: 'nothing found', detail: 'ok' },
    ]);
  });

  it('is empty for a run without skills, and renders nothing', () => {
    const bare = { tools: [] };
    expect(runSkills(bare)).toEqual({ discoveries: [], uses: [] });
    expect(render(RunSkills, { props: { run: bare } }).body).not.toContain('Skills');
  });

  it('renders the run’s skills as text', () => {
    const html = render(RunSkills, { props: { run } }).body;
    expect(hostileMarkup(html)).toBe(false);
    expect(html).toContain('by words and meaning');
    expect(html).toContain('A model call saw the notice.');
    expect(html).toContain('No model call saw the notice.');
  });

  it('names how each scoring went', () => {
    expect(scoringText('partial')).toBe('by words, and by meaning for some packs');
    expect(scoringText('failed')).toBe('not compared: it failed');
    expect(scoringText(7)).toBe('?');
  });
});

describe('the skills page', () => {
  it('renders a pin’s report as text, marking derived items', () => {
    const html = render(SkillPinCard, { props: { pin, projectId: 'p1' } }).body;
    expect(hostileMarkup(html)).toBe(false);
    expect(html).toContain('Derived by small-1 (alias fast), cached');
    expect(html).toContain('main at 0123456789ab');
    expect(html).toContain('plan');
    expect(html).not.toContain('other');
    expect(html).toMatch(/class="mark[^"]*">derived</);
  });

  it('renders the skills a run is given, marked by who set them', () => {
    const skill = (fields: Partial<SkillView>): SkillView => ({
      id: 'ss/alpha',
      source: 'ss',
      name: 'alpha',
      description: HOSTILE,
      revision: 'abcdef0123456789',
      short: null,
      topic: null,
      entry: false,
      short_by: null,
      topic_by: null,
      ...fields,
    });
    const skills = [
      skill({ short: 'Says.', short_by: 'derived', topic: 'Text', topic_by: 'user', entry: true }),
      skill({ id: 'builtin/plan', source: 'builtin', name: 'plan', description: 'Plans​.' }),
    ];
    const html = render(SkillTable, { props: { skills, always: ['builtin/plan'] } }).body;
    expect(hostileMarkup(html)).toBe(false);
    expect(html).toMatch(/class="mark[^"]*">derived</);
    expect(html).toMatch(/class="mark[^"]*">yours</);
    expect(html).toMatch(/class="mark[^"]*">entry</);
    expect(html).toMatch(/class="mark[^"]*">always loaded</);
    expect(html).toContain('U+200B');
  });

  it('reads the user’s overrides', () => {
    const view = overridesView({
      skills: { 'ss/alpha': { description: 'Short.', entry: true } },
      unbound: { ss: ['DATA'] },
      approved: { ss: ['LIBFOO_DIR'] },
      prefixes: { ss: 'LIBFOO_' },
    });
    expect(view.skills).toEqual([
      { skill: 'ss/alpha', fields: ['description: Short.', 'an entry skill'] },
    ]);
    expect(view.unbound).toEqual([{ source: 'ss', variables: ['DATA'] }]);
    expect(view.approved).toEqual([{ source: 'ss', variables: ['LIBFOO_DIR'] }]);
    expect(view.prefixes).toEqual([{ source: 'ss', prefix: 'LIBFOO_' }]);
    expect(overridesView({})).toEqual({ skills: [], unbound: [], approved: [], prefixes: [] });
  });

  it('takes skill IDs one a line, once each', () => {
    expect(idLines(' ss/a \n\nss/b\nss/a\n')).toEqual(['ss/a', 'ss/b']);
  });

  it('has a route', () => {
    expect(parseRoute(skillsHref('p1'))).toEqual({ view: 'skills', project: 'p1' });
  });
});
