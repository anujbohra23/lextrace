export type AuthorityRelationship = {
  case_id: string; forum_court: string | null; authority_court: string | null;
  category: string; rationale: string; hierarchy_path: string[]; uncertainty: string[];
};
export type CitationContext = {
  citing_case_id: string; cited_case_id: string; citing_opinion_id: string;
  passage: { passage_id: string; opinion_id: string; start: number; end: number; text: string };
  source_url: string; matched_text: string; confidence: string;
};
export type Treatment = {
  annotation_id: string; generated_label: string; review_state: string;
  review_requirement: string; confidence: string; passage: CitationContext | null;
};
export type TraceNode = {
  node: { case_id: string; case_name: string | null; court: string | null;
    date_filed: string | null; source_url: string | null };
  authority: AuthorityRelationship;
  relevant_passage: { passage_id: string; text: string } | null;
  relevance_score: number | null;
};
export type TraceEdge = {
  edge: { citing_case_id: string; cited_case_id: string;
    supports: { citing_opinion_id: string; cited_opinion_id: string; provenance_id: string }[] };
  context: CitationContext | null; treatment: Treatment;
};
export type PrecedentTrace = {
  seed_case_id: string; nodes: TraceNode[]; edges: TraceEdge[];
  nodes_examined: number; edges_examined: number; context_recovered: number;
  treatment_specific: number; treatment_fallback: number; warnings: string[];
};
export type DoctrineEvent = {
  event_id: string; category: string; case_id: string; case_name: string | null;
  court: string | null; date_filed: string; annotation_id: string;
  supporting_passage: CitationContext; uncertainty: string[];
};
export type DoctrineAnalysis = {
  trace: PrecedentTrace;
  events: DoctrineEvent[];
  state: { proposition: string; latest_relevant_case_id: string | null;
    controlling_authority_ids: string[];
    synthesis: { text: string; annotation_ids: string[] }[];
    coverage_warning: string };
  impact: { category: string; explanation: string; research_gaps: string[] };
};
export type ClaimAuthorityAnalysis = {
  relationships: AuthorityRelationship[]; warning: string;
};
