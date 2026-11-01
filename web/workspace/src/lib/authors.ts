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
    case 'worker':
      return `worker code in environment ${author.environment_id ?? '?'}, incarnation ${author.incarnation ?? '?'}${author.session_id ? `, session ${author.session_id}` : ''}`;
    case 'agent': {
      const role = author.role && author.role !== 'main' ? `${author.role} ` : '';
      const model = author.model ? `, ${author.model}` : '';
      return `${role}agent run ${author.run_id ?? '?'} of session ${author.session_id ?? '?'}${model}`;
    }
  }
}

/** Who wrote a revision, briefly, for a list: `Research agent · session main`, `You`. */
export function authorName(author: Author): string {
  switch (author.kind) {
    case 'user':
      return 'You';
    case 'import':
      return 'An import';
    case 'platform':
      return 'The platform';
    case 'worker':
      return 'Worker code';
    case 'agent': {
      const role = !author.role || author.role === 'main' ? 'Research' : author.role;
      const name = `${role[0]!.toUpperCase()}${role.slice(1)} agent`;
      return author.session_id ? `${name} · session ${author.session_id}` : name;
    }
  }
}
