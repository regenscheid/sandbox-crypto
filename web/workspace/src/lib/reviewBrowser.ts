// SPDX-License-Identifier: MIT
/**
 * The consistency reviews panel's reading, apart from its view (ReviewsPanel.svelte): the
 * session's reviews, findings and math checks, each a paged list; a chosen review's
 * snapshot evidence and audit records; and the slices of one evidence text or math check
 * being read. Choosing another review or text drops whatever was on its way for the last
 * one (pagedList.ts).
 */
import type { Json } from './api';
import { EvidenceText } from './evidenceText';
import { emptyList, PagedList, type ListState } from './pagedList';
import {
  reviews,
  type Evidence,
  type Finding,
  type MathJob,
  type MathQueue,
  type Review,
} from './reviews';

export interface ReviewPanel {
  reviews: ListState<Review, string>;
  /** Windows left unreviewed after bounded retries, from the reviews' first page. */
  unreviewed: Json[];
  /** Why the session's next review waits, when it does (lab 38). */
  waiting: Json;
  /** Why the session's reviews are off, when they are (lab 43's B6). */
  off: Json;
  findings: ListState<Finding, string>;
  checks: ListState<MathJob, string>;
  /** Sources waiting for math checks, from the checks' first page. */
  queue: MathQueue[];
  queueMore: boolean;
  /** The review chosen, as the latest list or manifest shows it. */
  selected: Review | null;
  evidence: ListState<Evidence, number>;
  records: ListState<Json, number>;
  /** The evidence text being read, slice by slice. */
  text: ListState<string, number>;
  /** The math check whose evidence is being read, page by page as JSON text. */
  mathCheck: string;
  math: ListState<string, number>;
  notice: string;
  requestError: unknown;
}

export function emptyReviewPanel(): ReviewPanel {
  return {
    reviews: emptyList(),
    unreviewed: [],
    waiting: null,
    off: null,
    findings: emptyList(),
    checks: emptyList(),
    queue: [],
    queueMore: false,
    selected: null,
    evidence: emptyList(),
    records: emptyList(),
    text: emptyList(),
    mathCheck: '',
    math: emptyList(),
    notice: '',
    requestError: null,
  };
}

/** Whether a list or a chosen review is being read; the panel's buttons wait for it. */
export function panelBusy(panel: ReviewPanel): boolean {
  return [panel.reviews, panel.findings, panel.checks, panel.evidence, panel.records].some(
    (list) => list.loading,
  );
}

export class ReviewBrowser {
  readonly reviews: PagedList<Review, string>;
  readonly findings: PagedList<Finding, string>;
  readonly checks: PagedList<MathJob, string>;
  readonly evidence: PagedList<Evidence, number>;
  readonly records: PagedList<Json, number>;
  readonly text: EvidenceText;
  readonly math: PagedList<string, number>;
  #review = '';
  #askedCheck = '';

  constructor(
    readonly panel: ReviewPanel,
    /** The session read, as the view's properties name it now. */
    private readonly where: () => { project: string; session: string },
    private readonly api: typeof reviews = reviews,
  ) {
    const at = () => {
      const { project, session } = where();
      return [project, session] as const;
    };
    this.reviews = new PagedList(panel.reviews, async (after) => {
      const page = await api.list(...at(), after);
      return {
        items: page.reviews,
        next: page.next_after,
        shown: () => {
          if (after !== null) return;
          panel.unreviewed = page.unreviewed;
          panel.waiting = page.waiting ?? null;
          panel.off = page.off ?? null;
          const chosen = panel.selected;
          if (chosen)
            panel.selected =
              page.reviews.find((item) => item.review_id === chosen.review_id) ?? chosen;
        },
      };
    });
    this.findings = new PagedList(panel.findings, async (after) => {
      const page = await api.findings(...at(), after);
      return { items: page.findings, next: page.next_after };
    });
    this.checks = new PagedList(panel.checks, async (after) => {
      const page = await api.math(...at(), after);
      return {
        items: page.checks,
        next: page.next_after,
        shown: () => {
          if (after !== null) return;
          panel.queue = page.queue;
          panel.queueMore = !!page.queue_more;
        },
      };
    });
    // A review's manifest is its evidence list's first page.
    this.evidence = new PagedList(panel.evidence, async (after) => {
      const page = await api.manifest(...at(), this.#review, after ?? 0);
      return {
        items: page.evidence,
        next: page.next_after,
        shown: () => {
          panel.selected = page.review;
          // The audit records can be read once the manifest is in.
          if (after === null) Object.assign(panel.records, { items: [], next: 0 });
        },
      };
    });
    this.records = new PagedList(panel.records, async (after) => {
      const page = await api.records(...at(), this.#review, after ?? 0);
      return { items: page.records as unknown as Json[], next: page.next_after };
    });
    this.text = new EvidenceText(panel.text, (review, evidence, start) =>
      api.evidence(...at(), review, evidence, start),
    );
    // A math check asked for replaces the one shown only when its first page is shown, as
    // an evidence text does (evidenceText.ts).
    this.math = new PagedList(panel.math, async (after) => {
      const check = after === null ? this.#askedCheck : panel.mathCheck;
      const page = await api.mathEvidence(...at(), check, after ?? 0);
      const shown = after === null ? page : page.observations;
      return {
        items: [JSON.stringify(shown, null, 2)],
        next: page.next_after,
        shown: () => void (panel.mathCheck = check),
      };
    });
  }

  /**
   * The reviews, findings and math checks again, from their first pages. Each list keeps
   * what it had when its own read fails; the others are shown as they arrive.
   */
  async refresh(): Promise<void> {
    await Promise.all([this.reviews.reload(), this.findings.reload(), this.checks.reload()]);
  }

  /**
   * Choose a review by its ID, as a link names it (a quote's citation check): its manifest
   * names the review when it arrives, and the panel shows it then.
   */
  open(review: string): Promise<void> {
    this.#review = review;
    this.panel.selected = null;
    this.records.clear();
    this.text.list.clear();
    this.evidence.clear();
    return this.evidence.reload();
  }

  /** Choose a review: its manifest and evidence, with its audit records ready to read. */
  select(review: Review): Promise<void> {
    this.#review = review.review_id;
    this.panel.selected = review;
    this.records.clear();
    this.text.list.clear();
    this.evidence.clear(); // the last review's evidence and cursor never serve this one
    return this.evidence.reload();
  }

  /** Read an evidence text from its start, in place of the one being read. */
  read(review: string, evidence: string): Promise<void> {
    return this.text.read(review, evidence);
  }

  /** Read a math check's evidence from its first page, in place of the one being read. */
  readMath(check: string): Promise<void> {
    this.#askedCheck = check;
    return this.math.reload();
  }

  /** Forget every failure shown, as the Refresh button does before refreshing. */
  clearErrors(): void {
    const { panel } = this;
    for (const list of [panel.evidence, panel.records, panel.text, panel.math]) list.error = null;
    panel.requestError = null;
  }

  /** Queue a review for the next live safe point. */
  async request(): Promise<void> {
    this.panel.requestError = null;
    try {
      const { project, session } = this.where();
      this.panel.notice = (await this.api.request(project, session)).detail;
    } catch (caught) {
      this.panel.requestError = caught;
    }
  }
}
