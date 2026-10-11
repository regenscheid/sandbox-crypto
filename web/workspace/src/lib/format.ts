// SPDX-License-Identifier: MIT

/** A time as the reader's locale writes it, to the second; the ISO text when unreadable. */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '';
  const time = new Date(iso);
  if (Number.isNaN(time.getTime())) return iso;
  return time.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' });
}

/** How long from one time to another, in words such as "4 s" or "2 min 5 s". */
export function duration(from: string, to: string): string {
  const seconds = Math.round((new Date(to).getTime() - new Date(from).getTime()) / 1000);
  if (!Number.isFinite(seconds) || seconds < 0) return '';
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ${seconds % 60} s`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

/** A policy revision shortened for tables: `sha256:` and the first 12 hex digits. */
export function shortRevision(revision: string | null | undefined): string {
  if (!revision) return '';
  const match = /^(sha256:)([0-9a-f]{12})[0-9a-f]+$/.exec(revision);
  return match ? `${match[1]}${match[2]}…` : revision;
}
