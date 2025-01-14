import type { CrawlState } from "../types/crawler";
export const activeCrawlStates: CrawlState[] = [
  "starting",
  "waiting_org_limit",
  "waiting_capacity",
  "running",
  "stopping",
];
export const inactiveCrawlStates: CrawlState[] = [
  "complete",
  "canceled",
  "partial_complete",
  "timed_out",
  "failed",
];

export function isActive(state: CrawlState) {
  return activeCrawlStates.includes(state);
}
