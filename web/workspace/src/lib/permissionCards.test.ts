// SPDX-License-Identifier: MIT
// What the cards and the Grants panel say of requests and grants (D97).
import { describe, expect, it } from 'vitest';
import type { PendingApproval, ProjectGrant } from './api-types';
import { cardTitle, grantLabel, scopeChoices, summaryRows } from './permissionCards';

const base: PendingApproval = {
  seq: 1,
  kind: 'mcp',
  source: 'worker',
  server: 'papers',
  tool: 'submit',
  method: 'tools/call',
  policy: 'sha256:x',
  session: 's',
  run: null,
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
  hold: null,
  scopes: ['once', 'project'],
};

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

describe('git requests', () => {
  it('say whether research code pushes or fetches', () => {
    const http = {
      method: 'GET',
      host: 'git.example.org',
      port: 443,
      path: '/lab/none.git/info/refs',
      size: 0,
      content_type: null,
      fields: {},
      git_service: 'receive-pack' as const,
    };
    const push: PendingApproval = { ...base, kind: 'network', caller_kind: 'code', http };
    expect(cardTitle(push)).toBe('Let research code git push to git.example.org?');
    expect(summaryRows(push)[0]).toEqual(['Git', 'push (git-receive-pack)']);
    const fetch = { ...push, http: { ...http, git_service: 'upload-pack' as const } };
    expect(cardTitle(fetch)).toBe('Let research code git fetch from git.example.org?');
    expect(summaryRows(fetch)[0]).toEqual(['Git', 'fetch (git-upload-pack)']);
    // Any other request is shown by its method, as before.
    const plain = { ...push, http: { ...http, git_service: null } };
    expect(cardTitle(plain)).toBe('Let research code GET git.example.org?');
    expect(summaryRows(plain)[0]).toEqual(['Method', 'GET']);
  });
});
