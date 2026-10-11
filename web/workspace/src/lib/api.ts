// SPDX-License-Identifier: MIT
/**
 * The workspace API, version 0 (packages/workspace, api.py): read-only views of the
 * harness's projects, sessions and runs. The session cookie travels with every request;
 * there is nothing else to send.
 */

export interface ProjectSummary {
  project_id: string;
  profile?: string;
  policy?: string;
  default_environment_id?: string;
  created_at?: string;
  updated_at?: string;
  sessions?: number;
  /** Set instead of the rest when the project's record is damaged. */
  error?: string;
}

export interface Session {
  session_id: string;
  environment_id: string;
  dedicated: boolean;
  opened_at: string;
  closed_at: string | null;
}

export interface Project {
  project_id: string;
  profile: string;
  policy: string;
  default_environment_id: string;
  created_at: string;
  updated_at: string;
  sessions: (Session & { runs: number })[];
  environments: { environment_id: string; default: boolean; sessions: string[] }[];
}

export interface RunEntry {
  name: string;
  run_id: string;
  started_at: string;
}

export interface SessionDetail {
  project_id: string;
  session: Session;
  runs: RunEntry[];
}

export type Json = string | number | boolean | null | Json[] | { [key: string]: Json };

export interface ToolCallMessage {
  id: string;
  type: string;
  function: { name: string; arguments: string };
}

export interface Message {
  role: 'system' | 'user' | 'assistant' | 'tool';
  content?: string | null;
  tool_calls?: ToolCallMessage[];
  tool_call_id?: string;
}

/** A tool call's record: its name and arguments, and whatever its outcome recorded. */
export interface ToolRecord {
  tool: string;
  arguments: string;
  replayed?: boolean;
  error?: string;
  [key: string]: Json | undefined;
}

export interface RunRecord {
  run_id: string;
  context: {
    project_id: string;
    session_id: string;
    environment_id: string;
    policy_revision: string | null;
    actor: string;
  };
  task: string;
  started_at: string;
  ended_at: string;
  stopped: string;
  final_text: string | null;
  messages: Message[];
  tools: ToolRecord[];
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly kind: string,
    message: string,
  ) {
    super(message);
  }

  get loggedOut(): boolean {
    return this.status === 401;
  }
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`/api/v0${path}`, { headers: { Accept: 'application/json' } });
  if (!response.ok) {
    let kind = 'unavailable';
    let message = `the server answered ${response.status}`;
    try {
      const body = (await response.json()) as { error?: { kind?: string; message?: string } };
      kind = body.error?.kind ?? kind;
      message = body.error?.message ?? message;
    } catch {
      // Not JSON: keep the status.
    }
    throw new ApiError(response.status, kind, message);
  }
  return (await response.json()) as T;
}

const part = encodeURIComponent;

export const api = {
  projects: () => get<{ projects: ProjectSummary[] }>('/projects'),
  project: (project: string) =>
    get<{ project: Project }>(`/projects/${part(project)}`).then((found) => found.project),
  session: (project: string, session: string) =>
    get<SessionDetail>(`/projects/${part(project)}/sessions/${part(session)}`),
  run: (project: string, session: string, run: string) =>
    get<RunRecord>(`/projects/${part(project)}/sessions/${part(session)}/runs/${part(run)}`),
  async logout(): Promise<void> {
    const response = await fetch('/api/v0/logout', { method: 'POST' });
    if (!response.ok && response.status !== 401) {
      throw new ApiError(response.status, 'unavailable', 'logging out failed');
    }
  },
};
