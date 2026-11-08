/** Wire contracts for M8A. Reviewer identity is deliberately absent from requests. */
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type ApprovedVersion = { document_id: string; version_id: string };
export type Gate = "evidence" | "findings";
export type ReviewTarget = ApprovedVersion & {
  matter_id: string; run_id: string; kind: "event" | "assertion" | "mention"; record_id: string;
};
export type EvidenceReference = ApprovedVersion & {
  section_id: string; start: number; end: number; exact_quote: string; page?: number | null;
};
export type OriginalRecord = ApprovedVersion & {
  matter_id: string; run_id: string; event_id?: string; assertion_id?: string; mention_id?: string;
  description?: string; normalized_proposition?: string; mention_text?: string;
  evidence_references: EvidenceReference[];
};
export type ReviewRecord = {
  original: OriginalRecord; revision: number; review_state: string;
  date_correction?: Json; resolved_entity_id?: string | null;
};
export type InvestigationView = {
  matter_id: string; thread_id: string; run_id: string;
  stage: "RUNNING" | "WAITING_REVIEW" | "BLOCKED" | "CANCELED" | "APPROVED";
  errors: string[]; coverage: string[]; checkpoint: Gate | null;
  token: string | null; digest: string | null;
  review: { records: ReviewRecord[]; chronology: Json; findings: Json } | null;
  snapshot_id: string | null;
};
export type PublishedSnapshot = {
  snapshot_id: string; matter_id: string; run_id: string; thread_id: string;
  chronology_id: string; finding_ids: string[]; reviewer_id: string; meaning: string;
  source_dependency_digest: string; review_dependency_digest: string;
  artifact_digest: string | null; approval_digest: string; evidence_approval_digest: string;
  entity_links_digest: string; sources: Json[]; dependencies: Json[];
};
export type DateCorrection = {
  kind: "exact" | "approximate" | "interval" | "unknown";
  earliest: string | null; latest: string | null; precision: "day" | "month" | "year" | "unknown";
};
export type EvidenceDecision = {
  decision_id: string; timestamp: string; target: ReviewTarget; expected_revision: number;
  decision: "ACCEPT" | "REJECT" | "DEFER" | "EDIT"; explanation?: string;
  date_correction?: DateCorrection; resolved_entity_id?: string;
};
export type EntityLink = { account: ReviewTarget; mentions: ReviewTarget[] };
export type InvestigationDecision = {
  run_id: string; gate: Gate; token: string; digest: string; decision: "APPROVE" | "REJECT";
  acknowledge_incomplete_coverage: boolean; entity_links: EntityLink[]; evidence_decisions: EvidenceDecision[];
};

export function reviewTarget(record: ReviewRecord): ReviewTarget {
  const original = record.original;
  const kind = original.event_id ? "event" : original.assertion_id ? "assertion" : "mention";
  const recordId = original.event_id ?? original.assertion_id ?? original.mention_id;
  if (!recordId) throw new Error("Review record identity is unavailable.");
  return { matter_id: original.matter_id, document_id: original.document_id,
    version_id: original.version_id, run_id: original.run_id, kind, record_id: recordId };
}
export function targetKey(record: ReviewRecord): string {
  return JSON.stringify(reviewTarget(record));
}
export function recordLabel(record: ReviewRecord): string {
  return record.original.description ?? record.original.normalized_proposition ?? record.original.mention_text ?? "Source record";
}
export function investigationBase(matterId: string): string {
  return `/matters/${encodeURIComponent(matterId)}/investigations`;
}
export function decisionStorageKey(matterId: string, threadId: string): string {
  return `lextrace.investigationDecision:${encodeURIComponent(matterId)}:${encodeURIComponent(threadId)}`;
}
