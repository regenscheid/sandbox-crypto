// SPDX-License-Identifier: MIT
import { expect, it } from 'vitest';
import type { DraftTool } from './api-types';
import { editable, problems, rewriteYaml, ruleWords, widens } from './classifications';

const YAML = `# SPDX-License-Identifier: MIT
# DRAFT, proposed by a model from the server's tool list: review every class, rule
# and reason before checking it in. Only the accepted file decides calls (D96).
apiVersion: sandbox-science/v0alpha1
kind: ToolClassification
server: papers
prefix: ""
future_field: kept
tools:
  "scrape":
    class: read
    reason: "Fetches one page."
    sha256: ${'a'.repeat(64)}
    significant: {url: url}
    rules:
      - {argument: actions, present: true, class: browse}
    note: unknown to the view
  "crawl":
    class: read
    reason: "Crawls a site."
    sha256: ${'b'.repeat(64)}
`;

const tool = (name: string, extra: Partial<DraftTool> = {}): DraftTool => ({
  name,
  effect: 'read',
  reason: 'Fetches one page.',
  rules: [{ argument: 'actions', present: true, equals: null, effect: 'browse' }],
  significant: { url: 'url' },
  sha256: 'a'.repeat(64),
  pin_state: 'current',
  mentioned: true,
  escalates: false,
  current_effect: 'read',
  widening: false,
  widening_reasons: [],
  ...extra,
});

it('says rules in words', () => {
  expect(ruleWords({ argument: 'actions', present: true, equals: null, effect: 'write' })).toBe(
    'actions present → write',
  );
  expect(
    ruleWords({ argument: 'profile.saveChanges', present: null, equals: 'true', effect: 'write' }),
  ).toBe('profile.saveChanges = true → write');
});

it('keeps unedited YAML as it is, but for the DRAFT comment', () => {
  const written = rewriteYaml(YAML, new Map());
  expect(written).toEqual({
    yaml: YAML.split('\n').slice(0, 1).concat(YAML.split('\n').slice(3)).join('\n'),
  });
});

it('rewrites only the edited tool and keeps unknown fields', () => {
  const edit = editable(tool('scrape'));
  edit.effect = 'write';
  edit.reason = 'Saves "profiles" # sometimes';
  edit.rules = [{ argument: 'webhook', present: null, equals: '"yes"', effect: 'communicate' }];
  edit.significant = [];
  const written = rewriteYaml(YAML, new Map([['scrape', edit]]));
  expect('yaml' in written).toBe(true);
  const text = (written as { yaml: string }).yaml;
  expect(text).not.toContain('# DRAFT');
  expect(text).toContain('future_field: kept');
  expect(text).toContain(`  "scrape":
    class: write
    reason: "Saves 'profiles' ' sometimes"
    sha256: ${'a'.repeat(64)}
    rules:
      - {argument: webhook, equals: "yes", class: communicate}
    note: unknown to the view
  "crawl":
    class: read
    reason: "Crawls a site."`);
  expect(text).not.toContain('significant:');
  expect(text).not.toContain('actions');
});

it('adds fields a tool did not have and refuses what it cannot place', () => {
  const edit = editable(tool('crawl', { rules: [], significant: null }));
  edit.significant = [{ kind: 'url', path: 'url' }];
  const text = (rewriteYaml(YAML, new Map([['crawl', edit]])) as { yaml: string }).yaml;
  expect(text.trimEnd().split('\n').slice(-2)).toEqual([
    `    sha256: ${'b'.repeat(64)}`,
    '    significant: {url: url}',
  ]);
  expect(rewriteYaml(YAML, new Map([['missing', edit]]))).toEqual({
    error: "the draft's YAML lacks missing",
  });
  expect(
    rewriteYaml('tools: {scrape: {class: read}}\n', new Map([['scrape', edit]])),
  ).toHaveProperty('error');
});

it('checks edits as the loader would refuse them', () => {
  const edit = editable(tool('scrape'));
  edit.reason = '';
  edit.rules.push({ argument: 'bad path!', present: true, equals: null, effect: 'write' });
  expect(problems(edit)).toEqual([
    'a reason is required',
    '"bad path!" is not an argument path (names joined by ".")',
  ]);
});

it('checks a rule value as JSON text the loader takes', () => {
  const rule = (equals: string | null) => {
    const edit = editable(tool('scrape'));
    edit.rules = [{ argument: 'mode', present: null, equals, effect: 'write' }];
    return problems(edit);
  };
  expect(rule('true')).toEqual([]);
  expect(rule('3')).toEqual([]);
  expect(rule('"fast"')).toEqual([]);
  expect(rule(null)).toEqual(['the rule on mode needs a value']);
  expect(rule('fast')).toEqual(['the rule on mode has a value that is not JSON']);
  expect(rule('{"a": 1}')).toEqual(['the rule on mode needs a value']);
  expect(rule(JSON.stringify('say "hi"'))).toEqual([
    'the rule on mode has a quote or backslash, or is too long',
  ]);
});

it('estimates widenings of edits, and keeps the worker mark otherwise', () => {
  const draft = tool('scrape', { current_effect: 'write', effect: 'write', widening: true });
  expect(widens(editable(draft), draft, undefined)).toBe(true);
  const edit = editable(draft);
  edit.effect = 'admin';
  expect(widens(edit, draft, undefined)).toBe(false);
  edit.effect = 'read';
  expect(widens(edit, draft, undefined)).toBe(true);
  const current = { ...tool('scrape'), effect: 'read' as const };
  const dropped = editable(tool('scrape', { widening: false }));
  dropped.rules = [];
  expect(widens(dropped, tool('scrape'), current)).toBe(true);
});
