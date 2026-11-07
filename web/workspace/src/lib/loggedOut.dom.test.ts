// SPDX-License-Identifier: MIT
// Logging out of an Authentik login (D104): the outpost's token opens no session again
// until it expires, so the page points the user at Authentik's own sign-out, which the
// server's answer names; a login link's logout answers no body. A session the server
// ended (timed out, logged out elsewhere, a restart) lands on the same page.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from '../App.svelte';
import LoggedOut from '../views/LoggedOut.svelte';
import { api } from './api';
import { signedOut } from './session';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

const SIGN_OUT = 'https://auth.example.org/outpost.goauthentik.io/sign_out';

function page(
  signOut: string | null,
  reason: 'logout' | 'ended' = 'logout',
  login: 'token' | 'forward' | null = null,
) {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(LoggedOut, { target, props: { signOut, reason, login } }));
  flushSync();
  return target;
}

/** The page's words, with the markup's line breaks as single spaces. */
const words = (target: Element) => (target.textContent ?? '').replace(/\s+/g, ' ');

describe('the logged-out page', () => {
  it("links Authentik's sign-out after an Authentik login's logout", () => {
    const target = page(SIGN_OUT);
    const link = target.querySelector('a')!;
    expect(link.getAttribute('href')).toBe(SIGN_OUT);
    expect(link.textContent).toBe('sign out of Authentik');
    expect(words(target)).toContain('no longer opens a session here');
  });

  it('names the current login link otherwise, and links nothing but https', () => {
    for (const signOut of [null, 'javascript:alert(1)']) {
      const target = page(signOut);
      expect(target.querySelector('a')).toBeNull();
      expect(words(target)).toContain('Logged out.');
      expect(words(target)).toContain('open the current login link');
      expect(words(target)).toContain('sandbox-science-workspace login-url');
    }
  });

  it('says a session the server ended ended, and how to log in again', () => {
    const operator = page(null, 'ended', 'token');
    expect(words(operator)).toContain('The session ended.');
    expect(words(operator)).toContain('2 hours after its last request');
    expect(words(operator)).toContain('each link works once');
    expect(words(operator)).not.toContain('sign in through Authentik');
    const authentik = page(null, 'ended', 'forward');
    expect(words(authentik)).toContain('reload the page to sign in through Authentik');
    expect(words(authentik)).not.toContain('login link');
  });
});

describe('a session the server ended', () => {
  it('takes the whole page to the logged-out view, not an error in each view', async () => {
    const fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            error: { kind: 'policy_denied', message: 'not logged in: open the login URL' },
          }),
          { status: 401 },
        ),
    );
    vi.stubGlobal('fetch', fetch);
    const target = document.createElement('div');
    document.body.append(target);
    mounted.push(mount(App, { target }));
    await vi.waitFor(() => expect(target.textContent).toContain('The session ended.'));
    expect(signedOut()).toBe(true);
    expect(target.querySelector('#workspace-navigation')).toBeNull();
    expect(target.textContent).not.toContain('Not logged in.');
  });
});

describe('logging out', () => {
  function answering(response: Response) {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => response),
    );
  }

  it("returns the answer's sign-out for an Authentik login", async () => {
    answering(new Response(JSON.stringify({ sign_out: SIGN_OUT }), { status: 200 }));
    expect(await api.logout()).toEqual({ sign_out: SIGN_OUT });
  });

  it("returns none for the login link's logout, a session already ended, or odd answers", async () => {
    answering(new Response(null, { status: 204 }));
    expect(await api.logout()).toEqual({ sign_out: null });
    answering(new Response(JSON.stringify({ error: {} }), { status: 401 }));
    expect(await api.logout()).toEqual({ sign_out: null });
    answering(new Response(JSON.stringify({ sign_out: 'http://auth.example.org/x' })));
    expect(await api.logout()).toEqual({ sign_out: null });
  });
});
