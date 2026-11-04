// SPDX-License-Identifier: MIT
// Logging out of an Authentik login (D104): the outpost's token opens no session again
// until it expires, so the page points the user at Authentik's own sign-out, which the
// server's answer names; a login link's logout answers no body.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import LoggedOut from '../views/LoggedOut.svelte';
import { api } from './api';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

const SIGN_OUT = 'https://auth.example.org/outpost.goauthentik.io/sign_out';

function page(signOut: string | null) {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(LoggedOut, { target, props: { signOut } }));
  flushSync();
  return target;
}

describe('the logged-out page', () => {
  it("links Authentik's sign-out after an Authentik login's logout", () => {
    const target = page(SIGN_OUT);
    const link = target.querySelector('a')!;
    expect(link.getAttribute('href')).toBe(SIGN_OUT);
    expect(link.textContent).toBe('sign out of Authentik');
    expect(target.textContent).toContain('no longer opens a session here');
  });

  it('names the login URL otherwise, and links nothing but https', () => {
    for (const signOut of [null, 'javascript:alert(1)']) {
      const target = page(signOut);
      expect(target.querySelector('a')).toBeNull();
      expect(target.textContent).toContain('open the login URL');
    }
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
