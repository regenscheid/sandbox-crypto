// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import { parseRoute, projectHref, sessionHref } from './router';

describe('routes', () => {
  it.each([
    ['', { view: 'projects' }],
    ['#', { view: 'projects' }],
    ['#/', { view: 'projects' }],
    ['#/projects/p1', { view: 'project', project: 'p1' }],
    ['#/projects/p1/', { view: 'project', project: 'p1' }],
    ['#/projects/p1/sessions/a', { view: 'session', project: 'p1', session: 'a' }],
    ['#/projects/p%201', { view: 'project', project: 'p 1' }],
    ['#/projects/%E0%A4%A', { view: 'missing' }],
    ['#/projects', { view: 'missing' }],
    ['#/projects//sessions/a', { view: 'missing' }],
    ['#/projects/p1/runs/a', { view: 'missing' }],
    ['#/elsewhere', { view: 'missing' }],
  ])('%s', (hash, route) => {
    expect(parseRoute(hash)).toEqual(route);
  });

  it('round-trips the links it makes', () => {
    expect(parseRoute(projectHref('a/b?c'))).toEqual({ view: 'project', project: 'a/b?c' });
    expect(parseRoute(sessionHref('p1', 's#1'))).toEqual({
      view: 'session',
      project: 'p1',
      session: 's#1',
    });
  });
});
