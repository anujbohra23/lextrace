import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ResearchView, Result } from "./ResearchView";

vi.mock("next/navigation", () => ({useRouter: () => ({push: vi.fn()})}));
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

test("accepts research input and shows loading", async () => {
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => undefined)));
  render(<ResearchView />);
  fireEvent.change(screen.getByLabelText("Legal question or fact pattern"), {target: {value: "A legal question"}});
  fireEvent.click(screen.getByRole("button", {name: "Start research"}));
  expect(await screen.findByText("Researching…")).toBeDisabled();
});

test("renders evidence, verification, and safe source link", () => {
  render(<Result result={{run_id:"a".repeat(32),identified_issues:[{issue_id:"i",label:"Issue",description:"Description"}],relevant_cases:[{result:{case_id:"1",case_name:"Example v. State",court:"ca2",date_filed:"2020-01-01",reporter_citations:["1 F.3d 2"],source_url:"https://www.courtlistener.com/opinion/1/example/",final_score:1,relevant_passage:{passage_id:"p1",text:"Evidence text."},diagnostics:{citation_provenance:[]}}}],final_memo:null,verification_results:[{claim_id:"c",status:"SUPPORTED",explanation:"Passage supports claim."}],grounding_summary:{supported:1,total_substantive_claims:1,confidence:"HIGH"},trace:{status:"completed",provider:"fake",model:"fake",total_seconds:1,usage:{calls:1,input_tokens:1,output_tokens:1}}}} />);
  expect(screen.getByText("Example v. State")).toBeInTheDocument();
  expect(screen.getByText("SUPPORTED")).toBeInTheDocument();
  expect(screen.getByRole("link", {name:/View source/})).toHaveAttribute("href", "https://www.courtlistener.com/opinion/1/example/");
});
