import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { PrecedentPanel } from "./PrecedentPanel";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("separates authority, treatment, timeline, and exact evidence with review", async () => {
  const annotation = "annotation-1";
  const node = (id: string, name: string) => ({
    node: { case_id: id, case_name: name, court: "ca2", date_filed: "2010-01-01", source_url: null },
    authority: { case_id: id, category: "CONTROLLING", rationale: "Same circuit.", uncertainty: [] },
    relevant_passage: { passage_id: `p${id}`, text: "Protected activity." }, relevance_score: 1,
  });
  const context = { citing_case_id: "2", cited_case_id: "1", citing_opinion_id: "22",
    passage: { passage_id: "passage", opinion_id: "22", start: 0, end: 40,
      text: "We distinguish Alpha v. State, 123 F.3d 456." },
    source_url: "https://www.courtlistener.com/opinion/2/", matched_text: "123 F.3d 456", confidence: "EXACT_REPORTER" };
  const edge = { edge: { citing_case_id: "2", cited_case_id: "1", supports: [] }, context,
    treatment: { annotation_id: annotation, generated_label: "DISTINGUISHES", review_state: "UNREVIEWED",
      review_requirement: "REVIEW_REQUIRED", confidence: "HIGH", passage: context } };
  const trace = { seed_case_id: "1", nodes: [node("1", "Alpha v. State"), node("2", "Beta v. State")],
    edges: [edge], nodes_examined: 2, edges_examined: 1, context_recovered: 1,
    treatment_specific: 1, treatment_fallback: 0, warnings: ["Local corpus is incomplete."] };
  const doctrine = { trace,
    events: [{ event_id: "event", category: "RULE_DISTINGUISHED", case_id: "2", case_name: "Beta v. State",
      court: "ca2", date_filed: "2010-01-01", annotation_id: annotation,
      supporting_passage: context, uncertainty: ["Requires review."] }],
    state: { proposition: "Protected activity", latest_relevant_case_id: "2", controlling_authority_ids: ["2"],
      synthesis: [], coverage_warning: "Local corpus is incomplete." },
    impact: { category: "WEAKENS", explanation: "Based on explicit language.", research_gaps: [] } };
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => ({ ok: true,
    json: async () => init?.method === "PATCH" ? { ...edge.treatment, review_state: "CONFIRMED" }
      : url.endsWith("/doctrine") ? doctrine : { relationships: [{ case_id: "1", category: "CONTROLLING",
        rationale: "Same circuit.", uncertainty: [] }], warning: "Issue fit unknown." },
  } as Response));
  vi.stubGlobal("fetch", fetchMock);
  render(<PrecedentPanel matterId={"a".repeat(32)} claimId={"b".repeat(32)} />);
  await waitFor(() => expect(screen.getAllByText(/Local corpus is incomplete/).length).toBeGreaterThan(0));
  expect(screen.getByText(/Same circuit/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /Beta v. State.*DISTINGUISHES/ }));
  expect(screen.getByText("We distinguish Alpha v. State, 123 F.3d 456.")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining(`/treatments/${annotation}`), expect.objectContaining({ method: "PATCH" })
  ));
});
