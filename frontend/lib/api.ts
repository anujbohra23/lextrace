export const API_URL = process.env.NEXT_PUBLIC_LEXTRACE_API_URL ?? "http://localhost:8000";

// Provision per browser tab for the local pilot; never bundle an API secret.
export function operatorHeaders(): Record<string, string> {
  const token = typeof window === "undefined" ? null : window.sessionStorage.getItem("lextrace.operatorToken");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function downloadMatrix(matterId: string): Promise<void> {
  const response = await fetch(`${API_URL}/matters/${matterId}/evidence-matrix.csv`, {
    headers: operatorHeaders(), cache: "no-store",
  });
  if (!response.ok) throw new Error("LexTrace request failed.");
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = "evidence-matrix.csv";
  link.click();
  URL.revokeObjectURL(url);
}

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
  constructor(public readonly status: number) {
    super("LexTrace request failed.");
    this.name = "ApiError";
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(API_URL + path, {
    ...init,
    headers: { "content-type": "application/json", ...init?.headers, ...operatorHeaders() },
    cache: "no-store",
  });
  if (!response.ok) throw new ApiError(response.status);
  return response.json() as Promise<T>;
}
