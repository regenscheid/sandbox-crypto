// SPDX-License-Identifier: MIT
/**
 * Completion in the chat composer (B10, item 6), on the client alone: `@` offers the
 * session's artifacts by path, `#` the project's sessions by ID, `/` the project's skills
 * by name. A trigger counts at the start of the text or after white space, and its word
 * runs to the caret, so a space (or Escape) closes it. Each open reads its list once and
 * filters it as the user types; choosing an item replaces the trigger and its word with
 * plain text the agent can read (the path, the session ID, the skill's name) and a space.
 * Nothing else is sent: the message is the text in the box.
 */
import type { ArtifactListing, ProjectSkills, ProjectView } from './api-types';
import { get } from './http';
import { visible } from './text';

export type CompletionKind = 'artifacts' | 'sessions' | 'skills';

/** The character that opens each kind. */
export const TRIGGERS: Readonly<Record<string, CompletionKind>> = {
  '@': 'artifacts',
  '#': 'sessions',
  '/': 'skills',
};

/** The most matches the list shows. */
export const MAX_MATCHES = 8;

/** One item to choose: what is inserted, and what the list shows beside it. */
export interface CompletionOption {
  /** The plain text inserted: an artifact's path, a session's ID, a skill's name. */
  value: string;
  /** A few words about it, shown as text with hidden characters as code points. */
  detail: string;
}

/** A trigger and its word, up to the caret. */
export interface Trigger {
  kind: CompletionKind;
  /** Where the trigger character is. */
  start: number;
  /** The word typed after it so far. */
  query: string;
}

/** The trigger the caret is in, or null: one at the text's start or after white space. */
export function triggerAt(text: string, caret: number): Trigger | null {
  const before = text.slice(0, caret);
  const match = /(^|\s)([@#/])(\S*)$/u.exec(before);
  if (!match) return null;
  const start = caret - match[3]!.length - 1;
  return { kind: TRIGGERS[match[2]!]!, start, query: match[3]! };
}

/**
 * The options whose value has the query, ignoring case, those starting with it first,
 * each group in the list's order, at most `MAX_MATCHES`.
 */
export function matching(options: readonly CompletionOption[], query: string): CompletionOption[] {
  const wanted = query.toLowerCase();
  const found = options.filter((option) => option.value.toLowerCase().includes(wanted));
  const first = found.filter((option) => option.value.toLowerCase().startsWith(wanted));
  const rest = found.filter((option) => !option.value.toLowerCase().startsWith(wanted));
  return [...first, ...rest].slice(0, MAX_MATCHES);
}

/** The text with the trigger and its word replaced by `value` and a space, and the caret after. */
export function insertCompletion(
  text: string,
  trigger: Pick<Trigger, 'start'>,
  caret: number,
  value: string,
): { text: string; caret: number } {
  // The rest of the word the caret is in goes too; a space follows the value, once.
  const rest = text.slice(caret).replace(/^\S*/u, '');
  const after = rest.startsWith(' ') ? rest.slice(1) : rest;
  const inserted = `${value} `;
  return {
    text: text.slice(0, trigger.start) + inserted + after,
    caret: trigger.start + inserted.length,
  };
}

const part = encodeURIComponent;

/** The session's artifacts, newest first, by path: one page of the project's list. */
export async function sessionArtifacts(
  project: string,
  session: string,
): Promise<CompletionOption[]> {
  const listing = await get<ArtifactListing>(
    `/projects/${part(project)}/artifacts?${new URLSearchParams({ session })}`,
  );
  return listing.artifacts
    .filter((entry) => !entry.deleted)
    .map((entry) => ({ value: entry.path, detail: `${entry.kind}, revision ${entry.revision}` }));
}

/** The project's sessions, the most recently opened first, by ID. */
export async function projectSessions(project: string): Promise<CompletionOption[]> {
  const found = await get<{ project: ProjectView }>(`/projects/${part(project)}`);
  return [...found.project.sessions]
    .sort((a, b) => b.opened_at.localeCompare(a.opened_at))
    .map((item) => ({
      value: item.session_id,
      detail: `${item.closed_at ? 'closed' : 'open'} · ${item.runs} ${item.runs === 1 ? 'run' : 'runs'}`,
    }));
}

/** The skills a run of the project is given now, by name, from its skills catalog. */
export async function projectSkills(project: string): Promise<CompletionOption[]> {
  const found = await get<ProjectSkills>(`/projects/${part(project)}/skills`);
  return found.effective.map((skill) => ({
    value: skill.name,
    detail: skill.short ?? skill.description,
  }));
}

/** The composer's completion, as the view shows it. */
export interface CompletionState {
  trigger: Trigger | null;
  options: CompletionOption[];
  loading: boolean;
  error: string | null;
  /** The highlighted match. */
  active: number;
}

export function closedCompletion(): CompletionState {
  return { trigger: null, options: [], loading: false, error: null, active: 0 };
}

/** What each kind's list is called, for the listbox's label. */
export const LABELS: Readonly<Record<CompletionKind, string>> = {
  artifacts: "This session's artifacts",
  sessions: "The project's sessions",
  skills: "The project's skills",
};

/**
 * The composer's completion: which trigger is open, its list read once per open, and the
 * highlighted match. The view makes the state with `$state` and calls `update` as the text
 * or the caret moves.
 */
export class Completer {
  /** Counts opens, so that a list read for an earlier open is dropped. */
  private opened = 0;
  /** The trigger the user closed with Escape: it stays closed until its word ends. */
  private dismissed: number | null = null;

  constructor(
    readonly state: CompletionState,
    private readonly sources: Readonly<Record<CompletionKind, () => Promise<CompletionOption[]>>>,
  ) {}

  /** The matches the list shows. */
  matches(): CompletionOption[] {
    return this.state.trigger ? matching(this.state.options, this.state.trigger.query) : [];
  }

  /** Follows the text and the caret: opens a trigger, filters it, or closes it. */
  update(text: string, caret: number): void {
    const found = triggerAt(text, caret);
    if (found === null || found.start !== this.dismissed) this.dismissed = null;
    if (found === null || found.start === this.dismissed) {
      this.close();
      return;
    }
    const current = this.state.trigger;
    if (current && current.start === found.start && current.kind === found.kind) {
      if (current.query !== found.query) this.state.active = 0;
      this.state.trigger = found;
      return;
    }
    this.open(found);
  }

  private open(trigger: Trigger): void {
    const opened = ++this.opened;
    Object.assign(this.state, {
      trigger,
      options: [],
      loading: true,
      error: null,
      active: 0,
    });
    this.sources[trigger.kind]().then(
      (options) => {
        if (opened !== this.opened) return;
        this.state.options = options;
        this.state.loading = false;
      },
      (error: unknown) => {
        if (opened !== this.opened) return;
        this.state.error = error instanceof Error ? error.message : String(error);
        this.state.loading = false;
      },
    );
  }

  /** Closes the list; a list still being read is dropped. */
  close(): void {
    if (this.state.trigger === null) return;
    this.opened++;
    Object.assign(this.state, closedCompletion());
  }

  /** Escape: closes the list, and keeps this trigger closed while its word goes on. */
  dismiss(): void {
    this.dismissed = this.state.trigger?.start ?? null;
    this.close();
  }

  /** Moves the highlight by `step`, wrapping round. */
  move(step: number): void {
    const count = this.matches().length;
    if (count) this.state.active = (this.state.active + step + count) % count;
  }

  /** The text with the highlighted (or the given) match inserted, and the list closed. */
  choose(
    text: string,
    caret: number,
    index = this.state.active,
  ): { text: string; caret: number } | null {
    const trigger = this.state.trigger;
    const option = this.matches()[index];
    if (!trigger || !option) return null;
    this.close();
    return insertCompletion(text, trigger, caret, option.value);
  }
}

/** An option's words in the list, hidden characters as code points. */
export function optionText(option: CompletionOption): { value: string; detail: string } {
  const detail = option.detail.length > 80 ? `${option.detail.slice(0, 79)}…` : option.detail;
  return { value: visible(option.value), detail: visible(detail) };
}
