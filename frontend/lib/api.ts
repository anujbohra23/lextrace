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

export class ApiError extends Error {
  constructor(public status: number) {
    super(status === 429 ? "Research is busy. Open an existing run or try again later." : status === 503 ? "This service is unavailable. Check that the model and local index are available, then retry." : status === 422 ? "Check the entered fields and try again." : status === 404 ? "This item could not be found." : "The request could not be completed. Please retry.");
  }
}
export function safeError(error: unknown): string {
  return error instanceof ApiError ? error.message : "Cannot reach LexTrace. Check your connection and retry. Your saved work is retained.";
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(API_URL + path, {
    ...init,
    headers: { ...(init?.body ? { "content-type": "application/json" } : {}), ...init?.headers },
  });
  if (!response.ok) throw new ApiError(response.status);
  return response.json() as Promise<T>;
}
