// SPDX-License-Identifier: MIT

/** A time as the reader's locale writes it, to the second; the ISO text when unreadable. */
/**
 * A cost as the gateway reported it, in its currency, which for LiteLLM is US dollars:
 * two decimals from a cent, two significant digits below it.
 */
export function formatCost(value: number): string {
  if (value === 0) return '$0';
  return `$${value >= 0.01 ? value.toFixed(2) : value.toPrecision(2)}`;
}

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

/** Attachment sizes use binary units, matching the upload limit. */
export function formatFileSize(bytes: number): string {
  return bytes >= 1024 ** 2
    ? `${(bytes / 1024 ** 2).toFixed(1)} MiB`
    : `${(bytes / 1024).toFixed(1)} KiB`;
}
