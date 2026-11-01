// SPDX-License-Identifier: MIT
import { describe, it, expect } from 'vitest';
import { parseRevisionReferences } from './reviews';
describe('changed review evidence references', () => {
  it('retains exact document revisions and accepts an empty list', () => {
    const id = `doc-${'a'.repeat(24)}`;
    expect(parseRevisionReferences(`${id}:1 ${id}:42`)).toEqual([
      [id, 1],
      [id, 42],
    ]);
    expect(parseRevisionReferences('')).toEqual([]);
  });
  it.each([
    'doc-x:1',
    `doc-${'a'.repeat(24)}:0`,
    `doc-${'a'.repeat(24)}:1.5`,
    '../escape:1',
    `doc-${'a'.repeat(24)}:1000000000`,
  ])('refuses malformed or unbounded references %s', (value) =>
    expect(() => parseRevisionReferences(value)).toThrow(),
  );
  it('bounds the number of revision pins', () =>
    expect(() =>
      parseRevisionReferences(
        Array(17)
          .fill(`doc-${'a'.repeat(24)}:1`)
          .join(' '),
      ),
    ).toThrow());
});
