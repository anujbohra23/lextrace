export const API_URL = process.env.NEXT_PUBLIC_LEXTRACE_API_URL ?? "http://localhost:8000";

export type Evidence = {
  result: {
    case_id: string;
    case_name: string;
    court: string;
    date_filed: string | null;
    reporter_citations: string[] | null;
    source_url: string;
    final_score: number;
    relevant_passage: { passage_id: string; text: string };
    diagnostics: { citation_provenance: unknown[] };
  };
};

export type ResearchResult = {
  run_id: string;
  identified_issues: { issue_id: string; label: string; description: string }[];
  relevant_cases: Evidence[];
  final_memo: null | {
    analysis: string[];
    supporting_arguments: string[];
    counterarguments: string[];
    research_conclusion: string;
    disclaimer: string;
  };
  verification_results: { claim_id: string; status: string; explanation: string }[];
  grounding_summary: { supported: number; total_substantive_claims: number; confidence: string };
  trace: { status: string; provider: string; model: string; total_seconds: number; usage: { calls: number; input_tokens: number; output_tokens: number } };
};

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(API_URL + path, {
    ...init,
    headers: { "content-type": "application/json", ...init?.headers },
  });
  if (!response.ok) throw new Error("LexTrace request failed.");
  return response.json() as Promise<T>;
}
