// SPDX-License-Identifier: MIT
// A copied-number finding's two numbers are lined up only once the reader opens them.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import CopiedNumbers from '../views/CopiedNumbers.svelte';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
});

describe("a copied-number finding's two numbers", () => {
  it('are lined up once opened', () => {
    const target = document.createElement('div');
    document.body.append(target);
    mounted.push(
      mount(CopiedNumbers, { target, props: { stated: '1234567890', held: '1234567809' } }),
    );
    flushSync();
    expect(target.querySelector('mark')).toBeNull();
    const details = target.querySelector('details')!;
    details.open = true;
    details.dispatchEvent(new Event('toggle'));
    flushSync();
    expect([...target.querySelectorAll('mark')].map((mark) => mark.textContent)).toEqual([
      '9',
      '0',
      '0',
      '9',
    ]);
    expect(target.textContent).toContain('2 places differ');
  });
});
