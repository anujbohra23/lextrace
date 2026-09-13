import { API_URL } from "./api";

export type Span = {
  document_id: string; section_id: string; start: number; end: number; page: number | null;
};
export type Matter = {
  matter_id: string; name: string; court: string | null; jurisdiction: string | null;
  reference: string | null; status: string;
};
export type Document = {
  document_id: string; filename: string; document_type: string;
  ingestion_status: string; page_count: number | null; text_length: number;
  analysis_warnings: string[];
};
export type Claim = {
  claim_id: string; document_id: string; issue_id: string | null;
  exact_source_text: string; normalized_proposition: string; span: Span;
  verification_state: string; irrelevant: boolean; manually_edited: boolean;
};
export type Issue = { issue_id: string; label: string; uncertainty: string | null; section_ids: string[]; research_topics: string[] };
export type Evidence = {
  evidence_id: string; role: string; retrieval_query: string;
  result: { case_id: string; case_name: string; court: string; date_filed: string | null;
    reporter_citations: string[] | null; source_url: string;
    relevant_passage: { passage_id: string; opinion_id: string; text: string } };
};
export type Finding = {
  claim_id: string; issue_id: string | null; proposition: string; source: Span; vulnerability: string;
  verification_status: string; explanation: string; research_coverage: string;
  unresolved_citation_ids: string[]; warnings: string[];
  evidence: Evidence[];
  cited_authorities: { case_id: string; evidence_id: string | null; citation_id: string | null; relation: string; pinned: boolean; removed: boolean }[];
  counter_authorities: { evidence: Evidence }[];
};
export type DocumentDetail = { document: Document; text: string; sections: { section_id: string; text: string; span: Span }[] };

export async function uploadDocument(matterId: string, file: File): Promise<Document> {
  const body = new FormData();
  body.append("file", file);
  const response = await fetch(`${API_URL}/matters/${matterId}/documents`, {
    method: "POST", body,
  });
  if (!response.ok) throw new Error("Document upload failed.");
  return response.json() as Promise<Document>;
}
