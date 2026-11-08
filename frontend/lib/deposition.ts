import { EvidenceReference, Json, ReviewRecord } from "./investigations";

export type Question = {
  question_id: string; witness_id: string; category: string; wording: string;
  rationale: string; evidence_ids: string[]; order: number; group: string;
  disposition: "INCLUDE" | "REJECT" | "DEFER";
};
export type Witness = {
  witness_id: string; label: string; identity_basis: string; accounts: ReviewRecord[];
  related_participants: Json[]; chronology: Json[]; findings: Json[]; uncertainties: string[];
};
export type PlanRevision = {
  plan_id: string; revision: number; status: string; questions: Question[];
  reviewer_id: string; timestamp: string; decision_id: string; action: string;
  acknowledges_limitations: boolean;
};
export type PlanView = {
  basis: {
    plan_id: string; matter_id: string; thread_id: string; snapshot_id: string;
    artifact_digest: string; source_dependency_digest: string; review_dependency_digest: string;
    outcome: "FULLY_SOURCED" | "LIMITED" | "BLOCKED"; limitations: string[]; coverage: string[];
    witnesses: Witness[]; evidence: { evidence_id: string; reference: EvidenceReference & { page_start?: number | null; page_end?: number | null } }[];
    questions: Question[]; meaning: string;
  };
  current: PlanRevision;
};
export type PlanSummary = {
  plan_id: string; snapshot_id: string; revision: number; status: string;
  outcome: string; witness_count: number; question_count: number;
};
export type ReviewAction = "REVISE" | "APPROVE" | "REJECT" | "DEFER";
export function depositionPath(matterId: string, threadId: string) {
  return `/matters/${encodeURIComponent(matterId)}/investigations/${encodeURIComponent(threadId)}`;
}
