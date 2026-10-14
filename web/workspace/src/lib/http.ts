// SPDX-License-Identifier: MIT
/**
 * The one request function for the workspace API. Every request goes to this server's
 * own origin under `/api/v0`, refuses redirects and the cache, can carry a deadline and a
 * byte limit, and fails with an `ApiError`. Errors from the server are
 * `{"error": {"kind", "message", …}}`; a request that never got an answer has status 0.
 * A 401 ends the page's session (session.ts), and no request starts after that.
 */
import { onSignOut, signOut, signedOut } from './session';

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly kind: string,
    message: string,
    readonly details: Record<string, unknown> = {},
    /** The whole error object the server sent, for fields beyond kind and message. */
    readonly error: Record<string, unknown> = {},
  ) {
    super(message);
  }

  get loggedOut(): boolean {
    return this.status === 401;
  }
}

export interface RequestOptions {
  method?: 'GET' | 'POST';
  /** Sent as JSON. */
  body?: unknown;
  signal?: AbortSignal;
  deadlineMs?: number;
  /** The most bytes of the answer to read; a larger answer fails. */
  maxBytes?: number;
}

const ERROR_BYTES = 64 * 1024;

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

async function read(response: Response, limit: number | undefined, signal: AbortSignal) {
  if (!response.body) return '';
  const declared = response.headers.get('content-length');
  if (limit !== undefined && declared !== null && Number(declared) > limit)
    throw new ApiError(0, 'unavailable', 'the answer is too large');
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const chunk = await reader.read();
      if (signal.aborted) throw new ApiError(0, 'unavailable', 'the request was stopped');
      if (chunk.done) break;
      size += chunk.value.length;
      if (limit !== undefined && size > limit)
        throw new ApiError(0, 'unavailable', 'the answer is too large');
      chunks.push(chunk.value);
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.length;
  }
  return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
}

function failure(status: number, text: string): ApiError {
  let found: Record<string, unknown> | null = null;
  try {
    found = record(record(JSON.parse(text))?.error);
  } catch {
    // Not JSON: keep the status.
  }
  const kind = typeof found?.kind === 'string' ? found.kind : 'unavailable';
  const message =
    typeof found?.message === 'string' ? found.message : `the server answered ${status}`;
  return new ApiError(status, kind, message, record(found?.details) ?? {}, found ?? {});
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  if (signedOut()) throw new ApiError(401, 'policy_denied', 'signed out');
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (options.signal?.aborted) abort();
  options.signal?.addEventListener('abort', abort, { once: true });
  const offSignOut = onSignOut(abort);
  const deadline =
    options.deadlineMs === undefined ? undefined : setTimeout(abort, options.deadlineMs);
  try {
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    const response = await fetch(`/api/v0${path}`, {
      method: options.method ?? 'GET',
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal,
      redirect: 'error',
      cache: 'no-store',
    });
    if (controller.signal.aborted) throw new ApiError(0, 'unavailable', 'the request was stopped');
    if (!response.ok) {
      const error = failure(response.status, await read(response, ERROR_BYTES, controller.signal));
      if (error.loggedOut) signOut();
      throw error;
    }
    return JSON.parse(await read(response, options.maxBytes, controller.signal)) as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(
      0,
      'unavailable',
      controller.signal.aborted ? 'the request was stopped' : 'the server could not be reached',
    );
  } finally {
    clearTimeout(deadline);
    options.signal?.removeEventListener('abort', abort);
    offSignOut();
  }
}

export const get = <T>(path: string, options: Omit<RequestOptions, 'method' | 'body'> = {}) =>
  request<T>(path, options);

export const post = <T>(
  path: string,
  body: unknown,
  options: Omit<RequestOptions, 'method'> = {},
) => request<T>(path, { ...options, method: 'POST', body });

/** Refuse an answer that names another scope than the one the view asked about. */
export function inScope<T>(value: T, expected: Record<string, string>): T {
  const found = record(value);
  if (!found || Object.entries(expected).some(([key, wanted]) => found[key] !== wanted))
    throw new ApiError(0, 'unavailable', 'an answer for another scope');
  return value;
}
