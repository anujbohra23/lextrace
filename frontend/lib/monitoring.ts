export type MonitoringTarget = {
  target_id: string; target_type: "CLAIM" | "AUTHORITY" | "ISSUE" | "DOCTRINE" | "MATTER";
  target_reference_id: string; enabled: boolean; automatic: boolean;
  last_checked_at: string | null; last_corpus_version: string | null;
};
export type MonitoringRun = {
  run_id: string; status: string; outcome: string | null; completed_at: string | null;
  new_cases_examined: number; alerts_created: number; targets_examined: number;
  errors: string[];
};
export type MonitoringOverview = {
  targets: MonitoringTarget[]; recent_runs: MonitoringRun[];
  alert_count: number; unread_alert_count: number;
};
export type MatterAlert = {
  alert_id: string; claim_id: string; event_id: string; impact_id: string;
  new_case_id: string; severity: string; title: string; explanation: string;
  review_state: string; created_at: string; updated_at: string;
};
export type AlertDetail = {
  alert: MatterAlert;
  event: {
    event_type: string; new_case_id: string; affected_case_ids: string[];
    citation_provenance_ids: string[]; result: {
      case_name: string; court: string; source_url: string;
      relevant_passage: { passage_id: string; text: string; start: number; end: number };
    } | null;
    treatment: { generated_label: string; review_state: string; annotation_id: string } | null;
  } | null;
  impact: {
    category: string; explanation: string; previous_finding_status: string | null;
    previous_coverage: string | null; previous_doctrine: string | null;
    new_doctrine: string | null; authority_relationship: string;
    treatment_relationship: string | null; confidence: string;
  } | null;
};
