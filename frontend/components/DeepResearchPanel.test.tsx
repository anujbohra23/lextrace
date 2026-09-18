import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DeepResearchPanel } from "./DeepResearchPanel";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function json(value: unknown, ok = true) {
  return { ok, json: async () => value } as Response;
}

it("requires approval before starting bounded research and displays verified attacks", async () => {
  const gap = { gap_id: "gap", type: "NO_COUNTER_AUTHORITY", explanation: "No contrary authority recorded.", objective: "Search limits", priority: "MEDIUM", resolved: false };
  const step = { step_id: "step", claim_id: "claim", gap_id: "gap", intent: "COUNTER_AUTHORITY", query: "limiting cases", objective: "Search limits", retrieval_strategy: "reranked", max_results: 3, priority: "MEDIUM", approved: false };
  const plan = { plan_id: "plan", status: "DRAFT", steps: [step], max_rounds: 2, max_queries_per_round: 3, max_total_results: 30 };
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/research-coverage")) return json({ claim_id: "claim", category: "PARTIAL", queries_executed: 0, authorities_retrieved: 1, gaps: [gap], warning: "Indexed corpus only." });
    if (url.endsWith("/attacks")) return json([{ attack_id: "attack", attack_type: "NON_CONTROLLING_AUTHORITY", explanation: "Not controlling.", severity: "MEDIUM", confidence: "HIGH", authority_case_ids: ["1"], passage_ids: ["p-1"], remediation: "Review authority.", verified: true, verification_notes: [] }]);
    if (url.endsWith("/research-plan") && init?.method === "POST") return json(plan);
    if (url.endsWith("/research-plan") && init?.method === "PATCH") return json({ ...plan, status: "APPROVED", steps: [{ ...step, approved: true }] });
    if (url.endsWith("/research-plan")) return json({}, false);
    if (url.endsWith("/deep-research")) return json({ run_id: "run" });
    return json({});
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<DeepResearchPanel matterId="matter" claimId="claim" onUpdate={() => undefined} />);
  await waitFor(() => expect(screen.getByText("PARTIAL")).toBeInTheDocument());
  expect(screen.getByText(/No contrary authority recorded/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Create research plan" }));
  await waitFor(() => expect(screen.getByText(/Review research plan/)).toBeInTheDocument());
  expect(screen.getByRole("button", { name: "Investigate this claim" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Approve plan" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Investigate this claim" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Investigate this claim" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/deep-research"), expect.anything()));
  expect(screen.getByText(/Not controlling/)).toBeInTheDocument();
});
