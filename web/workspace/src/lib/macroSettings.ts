// SPDX-License-Identifier: MIT
/**
 * The project macro editor's decisions, apart from the view (ProjectMacros.svelte): the
 * draft is the setting as JSON text with the revision it was based on (PROJECT-MACROS.md).
 */
import type { MacroContext } from './api';
import type { MacroDraft } from './drafts';

function asText(macros: Record<string, string>): string {
  return JSON.stringify(macros, null, 2);
}

/**
 * The draft after reading the current setting: an empty draft takes it; one with text
 * keeps its text and its base, so a newer revision shows as a conflict to review.
 */
export function adoptCurrent(draft: MacroDraft, current: MacroContext): MacroDraft {
  return draft.text ? draft : { expected: current.revision, text: asText(current.macros) };
}

/** The change a draft sends; it throws when the text is not JSON. */
export function macroChange(draft: MacroDraft): Record<string, unknown> {
  return { expected: draft.expected, macros: JSON.parse(draft.text) as unknown };
}

/** The draft after a save: the setting as saved, based on its new revision. */
export function savedDraft(saved: MacroContext): MacroDraft {
  return { expected: saved.revision, text: asText(saved.macros) };
}
