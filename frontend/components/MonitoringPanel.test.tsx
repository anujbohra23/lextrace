import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MonitoringPanel } from "./MonitoringPanel";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function json(value: unknown) {
  return { ok: true, json: async () => value } as Response;
}

it("shows no-change history and inspectable alert evidence with review actions", async () => {
  const alert = { alert_id: "alert", claim_id: "claim", event_id: "event", impact_id: "impact", new_case_id: "2", severity: "HIGH", title: "New authority may affect claim", explanation: "Review the exact passage.", review_state: "UNREAD", created_at: "2025-01-01", updated_at: "2025-01-01" };
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/monitoring")) return json({ targets: [{ target_id: "target", target_type: "CLAIM", target_reference_id: "claim", enabled: true, automatic: true }], recent_runs: [{ run_id: "run", status: "completed", outcome: "NO_MATERIAL_CHANGE", completed_at: "2025-01-02", new_cases_examined: 1, alerts_created: 0 }], alert_count: 1, unread_alert_count: 1 });
    if (url.endsWith("/alerts")) return json([alert]);
    if (url.endsWith("/alerts/alert") && init?.method === "PATCH") return json({ ...alert, review_state: "REVIEWED" });
    if (url.endsWith("/alerts/alert")) return json({ alert, event: { event_type: "NEW_CITING_AUTHORITY", new_case_id: "2", affected_case_ids: ["1"], citation_provenance_ids: [], result: { case_name: "Synthetic Case 2", court: "ca2", source_url: "https://www.courtlistener.com/opinion/2/", relevant_passage: { passage_id: "p-2", text: "The exact synthetic passage.", start: 0, end: 28 } } }, impact: { category: "WEAKENS", explanation: "Synthetic contradiction.", previous_finding_status: "STRONG", previous_coverage: "PARTIAL", previous_doctrine: null, new_doctrine: null, authority_relationship: "CONTROLLING", treatment_relationship: null, confidence: "MEDIUM" } });
    return json({});
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<MonitoringPanel matterId="matter" onUpdate={() => undefined} openClaim={() => undefined} />);
  await waitFor(() => expect(screen.getByText(/NO_MATERIAL_CHANGE/)).toBeInTheDocument());
  expect(screen.getByText("New authority may affect claim")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Review impact" }));
  await waitFor(() => expect(screen.getByText("The exact synthetic passage.")).toBeInTheDocument());
  expect(screen.getByText(/Doctrine before:/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Mark reviewed" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/alerts/alert"), expect.objectContaining({ method: "PATCH" })));
});

it("offers bounded structured impact review for a manual run", async () => {
  const fetchMock = vi.fn(async (url: string) => {
    if (url.endsWith("/monitoring")) return json({ targets: [], recent_runs: [], alert_count: 0, unread_alert_count: 0 });
    if (url.endsWith("/alerts")) return json([]);
    return json({ run_id: "synthetic-run" });
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<MonitoringPanel matterId="matter" onUpdate={() => undefined} openClaim={() => undefined} />);
  await waitFor(() => expect(screen.getByText(/No monitoring runs yet/)).toBeInTheDocument());
  fireEvent.change(screen.getByLabelText("New corpus path"), { target: { value: "data/synthetic.jsonl" } });
  fireEvent.click(screen.getByLabelText(/Use configured impact model/));
  fireEvent.click(screen.getByRole("button", { name: "Run monitoring" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining("/monitoring/run"),
    expect.objectContaining({ method: "POST", body: expect.stringContaining('"max_llm_calls":2') }),
  ));
});
