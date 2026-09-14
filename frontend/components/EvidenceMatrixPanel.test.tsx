import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MatrixRow } from "@/lib/deepResearch";
import { EvidenceMatrixPanel } from "./EvidenceMatrixPanel";

afterEach(cleanup);

const base: MatrixRow = {
  claim_id: "claim-1", issue_id: "issue-1", issue: "Retaliation",
  claim: "Protected activity is required.", document_id: "doc-1",
  document_name: "brief.txt", source_start: 0, source_end: 20,
  matter_evidence_ids: [], cited_case_ids: ["1"], cited_source_urls: [], citation_support: "SUPPORTED",
  authority_category: "CONTROLLING", strongest_support_case_id: "1",
  strongest_counter_case_id: null, later_treatment: [],
  doctrine_state: "NOT_REVIEWED", coverage: "PARTIAL",
  gaps: ["NO_COUNTER_AUTHORITY"], attack_severity: "MEDIUM",
  attack_ids: ["attack"], argument_status: "MIXED", importance: "high",
};

it("filters coverage and opens a claim without conflating attack severity", () => {
  const open = vi.fn();
  render(<EvidenceMatrixPanel matterId="matter" rows={[
    base,
    { ...base, claim_id: "claim-2", claim: "Another claim.", coverage: "SUFFICIENT", attack_severity: null, gaps: [] },
  ]} openClaim={open} />);
  expect(screen.getByText("Protected activity is required.")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Coverage"), { target: { value: "SUFFICIENT" } });
  expect(screen.queryByText("Protected activity is required.")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Another claim." }));
  expect(open).toHaveBeenCalledWith("claim-2");
  expect(screen.getByRole("link", { name: "Export CSV" })).toHaveAttribute("href", expect.stringContaining("evidence-matrix.csv"));
});
