import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MatterList } from "./MatterList";
import { MatterWorkspace } from "./MatterWorkspace";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function json(value: unknown) {
  return { ok: true, json: async () => value } as Response;
}

it("creates a matter and links to its workspace", async () => {
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) =>
    init?.method === "POST"
      ? json({ matter_id: "a".repeat(32), name: "Smith v. Example", court: "S.D.N.Y." })
      : json([]));
  vi.stubGlobal("fetch", fetchMock);
  render(<MatterList />);
  fireEvent.change(screen.getByPlaceholderText("Smith v. Acme"), { target: { value: "Smith v. Example" } });
  fireEvent.click(screen.getByText("Create matter"));
  await waitFor(() => expect(screen.getByText("Smith v. Example")).toBeInTheDocument());
  expect(screen.getByRole("link", { name: /Smith v. Example/ })).toHaveAttribute("href", `/matters/${"a".repeat(32)}`);
});

it("shows Review evidence and highlights the exact source span", async () => {
  const matterId = "a".repeat(32);
  const source = "A legal proposition appears here. Extra text.";
  const span = { document_id: "b".repeat(32), section_id: "c".repeat(24), start: 0, end: 31, page: 1 };
  const claim = { claim_id: "d".repeat(32), document_id: span.document_id, issue_id: "e".repeat(32),
    exact_source_text: source.slice(0, 31), normalized_proposition: "A legal proposition appears here.",
    span, verification_state: "SUPPORTED", irrelevant: false, manually_edited: false };
  const evidence = { evidence_id: "f".repeat(32), role: "cited", retrieval_query: "legal proposition",
    result: { case_id: "1", case_name: "Smith v. Example", court: "ca2", date_filed: "2009-01-01",
      reporter_citations: ["123 F.3d 456"], source_url: "https://www.courtlistener.com/opinion/1/",
      relevant_passage: { passage_id: "p1", opinion_id: "11", text: "The exact precedent passage." } } };
  const finding = { claim_id: claim.claim_id, issue_id: claim.issue_id,
    proposition: claim.normalized_proposition, source: span, vulnerability: "STRONG",
    verification_status: "SUPPORTED", explanation: "Cited and independent support.",
    research_coverage: "SEARCHED", unresolved_citation_ids: [], warnings: [], evidence: [evidence],
    cited_authorities: [{ case_id: "1", relation: "SUPPORTS", pinned: false, removed: false }],
    counter_authorities: [] };
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.endsWith("/documents")) return json([{ document_id: span.document_id, filename: "brief.txt", document_type: "txt", ingestion_status: "READY", page_count: null, text_length: source.length }]);
    if (url.endsWith(`/documents/${span.document_id}`)) return json({ document: { document_id: span.document_id }, text: source, sections: [] });
    if (url.endsWith("/alerts")) return json([]);
    if (url.endsWith("/monitoring")) return json({targets: [], unread_alert_count: 0, recent_runs: []});
    if (url.endsWith("/evidence-matrix")) return json([{claim_id: claim.claim_id, claim: claim.normalized_proposition, document_id: span.document_id, document_name: "brief.txt", issue: "Issue", gaps: [], coverage: "PARTIAL", argument_status: "STRONG", authority_category: "UNKNOWN", citation_support: "SUPPORTED", strongest_support_case_id: "1", attack_severity: null}]);
    if (url.endsWith("/claims")) return json([claim]);
    if (url.endsWith("/issues")) return json([{ issue_id: claim.issue_id, label: "Issue", section_ids: [], research_topics: [], uncertainty: null }]);
    if (url.endsWith("/argument-xray")) return json([finding]);
    return json({ matter_id: matterId, name: "Test matter", court: "ca2" });
  }));
  render(<MatterWorkspace matterId={matterId} />);
  await waitFor(() => expect(screen.getByText("Test matter")).toBeInTheDocument());
  fireEvent.click(screen.getByRole("tab", { name: "Review" }));
  fireEvent.click(screen.getByRole("button", { name: "A legal proposition appears here." }));
  await waitFor(() => expect(screen.getByText("The exact precedent passage.")).toBeInTheDocument());
  expect(screen.getByText(source.slice(0, 31))).toHaveProperty("tagName", "MARK");
  expect(screen.getAllByText("Smith v. Example")).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", {name: "Close"}));
  fireEvent.click(screen.getByRole("button", {name: "Export review report"}));
  expect((screen.getByLabelText("Report text") as HTMLTextAreaElement).value).toContain("The exact precedent passage.");
  expect(screen.getByRole("link", {name: "Download Markdown report"})).toHaveAttribute("download", "lextrace-review.md");
});
