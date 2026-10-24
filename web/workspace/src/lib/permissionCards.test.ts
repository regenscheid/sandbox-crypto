// SPDX-License-Identifier: MIT
// What the cards and the Grants panel say of requests and grants (D97).
import { describe, expect, it } from 'vitest';
import type { PendingApproval, ProjectGrant } from './api-types';
import { cardTitle, grantLabel, scopeChoices, summaryRows } from './permissionCards';

const base: PendingApproval = {
  seq: 1,
  kind: 'mcp',
  source: 'proxy',
  server: 'papers',
  tool: 'submit',
  method: 'tools/call',
  policy: 'sha256:x',
  session: 's',
  rule: null,
  reason: null,
  recorded_at: '2026-10-24T12:00:00Z',
  expires_at: '2026-10-24T12:30:00Z',
  timed_out: false,
  on_timeout: 'deny',
  child: null,
  caller_kind: 'agent',
  effect: 'write',
  max_grant: 'project',
  summary: {},
  http: null,
  packages: null,
  scopes: ['once', 'project'],
};

describe('cards of held tool calls', () => {
  it('read alike whether the MCP proxy or the policy middleware holds the call', () => {
    const direct: PendingApproval = { ...base, source: 'worker', summary: { title: 'm-1' } };
    const proxied: PendingApproval = { ...direct, source: 'proxy' };
    expect(cardTitle(direct)).toBe(cardTitle(proxied));
    expect(summaryRows(direct)).toEqual(summaryRows(proxied));
    expect(scopeChoices(direct)).toEqual(scopeChoices(proxied));
  });
});

describe('scope choices', () => {
  it('follow the scopes the server offers, and always include once', () => {
    expect(scopeChoices(base).map((choice) => choice.id)).toEqual(['once', 'project']);
    expect(scopeChoices({ ...base, scopes: [] }).map((choice) => choice.id)).toEqual(['once']);
    expect(scopeChoices({ ...base, scopes: ['once', 'forever'] }).map((c) => c.id)).toEqual([
      'once',
    ]);
  });
});

describe('titles and grant labels', () => {
  it('name the tool, the endpoint or the packages', () => {
    expect(cardTitle(base)).toBe('Use submit?');
    expect(cardTitle({ ...base, kind: 'packages', packages: null })).toBe(
      'Install these packages?',
    );
    const grant: ProjectGrant = {
      grant_id: 'g',
      kind: 'network',
      scope: 'project',
      session: null,
      server: null,
      effect: null,
      host: 'data.org',
      methods: 'read',
      ecosystem: null,
      channel: null,
      created_at: '2026-10-24T12:00:00Z',
      expires_at: null,
      last_used_at: null,
      uses: 0,
    };
    expect(grantLabel(grant)).toBe("Research code's reads from data.org");
    expect(grantLabel({ ...grant, kind: 'mcp', server: 'papers', effect: 'write' })).toBe(
      'write calls to papers',
    );
  });
});
