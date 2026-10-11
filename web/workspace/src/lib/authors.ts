// SPDX-License-Identifier: MIT
/** Who wrote a revision, in words (living documents, LD-10, LD-11). */
import type { Author } from './api';

export function describeAuthor(author: Author): string {
  switch (author.kind) {
    case 'user':
      return 'the user';
    case 'import':
      return 'an import by the user';
    case 'platform':
      return `the platform's ${author.service ?? 'service'}`;
    case 'agent': {
      const role = author.role && author.role !== 'main' ? `${author.role} ` : '';
      const model = author.model ? `, ${author.model}` : '';
      return `${role}agent run ${author.run_id ?? '?'} of session ${author.session_id ?? '?'}${model}`;
    }
  }
}

/** The blocks a revision changed, in words: `changed b3, b4; added b9`. */
export function describeChanges(changes: {
  changed: readonly string[];
  added: readonly string[];
  moved: readonly string[];
  removed: readonly string[];
}): string {
  const parts: string[] = [];
  for (const status of ['changed', 'added', 'moved', 'removed'] as const) {
    const ids = changes[status];
    if (ids.length > 0) parts.push(`${status} ${ids.join(', ')}`);
  }
  return parts.join('; ') || 'no blocks';
}
