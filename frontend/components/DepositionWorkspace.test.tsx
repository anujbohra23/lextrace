import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { DepositionWorkspace } from "./DepositionWorkspace";
import { PlanView } from "@/lib/deposition";

const { push } = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
const root = "/matters/matter/investigations/thread";
const base = root + "/deposition-plans";
const question = { question_id: "q", witness_id: "w", category: "FOUNDATION", wording: "What is the basis of this account?", rationale: "Clarify the attributed source.", evidence_ids: ["e"], order: 0, group: "", disposition: "INCLUDE" as const };
const view: PlanView = {
  basis: { plan_id: "plan", matter_id: "matter", thread_id: "thread", snapshot_id: "snapshot", artifact_digest: "artifact", source_dependency_digest: "source-digest", review_dependency_digest: "review-digest", outcome: "LIMITED", limitations: ["INCOMPLETE_COVERAGE", "CANDIDATES_QUARANTINED"], coverage: ["PARTIAL"], meaning: "DEPOSITION_PREPARATION_NOT_INVESTIGATION_SIGNOFF",
    witnesses: [{ witness_id: "w", label: "Morgan", identity_basis: "SOURCE_ACCOUNT_ATTRIBUTION", accounts: [], related_participants: [], chronology: [], findings: [], uncertainties: ["SPEAKER_IDENTITIES_NOT_MERGED_BY_NAME"] }],
    evidence: [{ evidence_id: "e", reference: { document_id: "doc", version_id: "version", section_id: "section", start: 0, end: 19, exact_quote: "Exact witness quote" } }], questions: [question] },
  current: { plan_id: "plan", revision: 1, status: "DRAFT", questions: [question], reviewer_id: "operator", timestamp: "2020-01-01T00:00:00Z", decision_id: "create", action: "CREATE", acknowledges_limitations: false },
};
function json(value: unknown, status = 200) { return { ok: status >= 200 && status < 300, status, json: async () => value } as Response; }
function state(revision: number, status = "DRAFT"): PlanView { return { ...view, current: { ...view.current, revision, status, action: status === "APPROVED" ? "APPROVE" : "REVISE" } }; }
function posts(mock: ReturnType<typeof vi.fn>) { return mock.mock.calls.filter(([, init]) => init?.method === "POST"); }
beforeEach(() => { push.mockClear(); sessionStorage.clear(); sessionStorage.setItem("lextrace.operatorToken", "tab-token"); });
afterEach(() => { cleanup(); sessionStorage.clear(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("creates only from the current signed snapshot and authenticates every request", async () => {
  const mock = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") return json(view);
    if (url.endsWith("/deposition-plans")) return json([]);
    return json({ stage: "APPROVED", coverage: ["PARTIAL"], snapshot_id: "snapshot" });
  }); vi.stubGlobal("fetch", mock);
  render(<DepositionWorkspace matterId="matter" threadId="thread" />);
  const create = screen.getByRole("button", { name: "Create deposition plan" });
  await waitFor(() => expect(create).toBeEnabled()); fireEvent.click(create);
  await waitFor(() => expect(push).toHaveBeenCalledWith(`${root}/deposition/plan`));
  expect(JSON.parse(posts(mock)[0][1].body)).toMatchObject({ snapshot_id: "snapshot" });
  expect(JSON.parse(posts(mock)[0][1].body).request_id).toBeTruthy();
  for (const [url, init] of mock.mock.calls) {
    expect(url).not.toContain("tab-token"); expect(new Headers(init?.headers).get("authorization")).toBe("Bearer tab-token");
  }
});

it("blocks creation from an unpublished investigation without inventing witnesses", async () => {
  const mock = vi.fn(async () => json({ stage: "WAITING_REVIEW", coverage: ["PARTIAL"], snapshot_id: null })); vi.stubGlobal("fetch", mock);
  render(<DepositionWorkspace matterId="matter" threadId="thread" />);
  await screen.findByText(/WAITING_REVIEW/);
  expect(screen.getByRole("button", { name: "Create deposition plan" })).toBeDisabled();
  expect(screen.getByText(/not permission to invent evidence/)).toBeInTheDocument();
  expect(posts(mock)).toHaveLength(0);
});

it("reviews source-linked questions and requires explicit partial-coverage approval", async () => {
  let current = view;
  const mock = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") { current = state(2, "APPROVED"); return json(current); }
    return json(url.endsWith("/history") ? [current.current] : current);
  }); vi.stubGlobal("fetch", mock);
  render(<DepositionWorkspace matterId="matter" threadId="thread" planId="plan" />);
  await screen.findByLabelText("Question 1");
  expect(screen.getByText("Morgan")).toBeInTheDocument();
  expect(screen.getByText(/CANDIDATES_QUARANTINED/)).toBeInTheDocument();
  expect(screen.getAllByText("Exact witness quote").length).toBeGreaterThan(0);
  expect(screen.getByText(/characters 0–19/)).toBeInTheDocument();
  const approve = screen.getByRole("button", { name: "Approve deposition plan" });
  expect(approve).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: /I confirm approval/ })); expect(approve).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: /I acknowledge the partial coverage/ }));
  fireEvent.change(screen.getByLabelText("Question 1"), { target: { value: "Could you clarify the basis of the quoted account?" } });
  fireEvent.change(screen.getByLabelText("Rationale 1"), { target: { value: "Clarify source interpretation." } });
  fireEvent.change(screen.getByLabelText("Optional preparation group 1"), { target: { value: "Morning foundation" } });
  fireEvent.click(approve);
  await waitFor(() => expect(posts(mock)).toHaveLength(1));
  const request = JSON.parse(posts(mock)[0][1].body).review;
  expect(request).toMatchObject({ expected_revision: 1, action: "APPROVE", confirm: true, acknowledge_limitations: true });
  expect(request.questions[0]).toMatchObject({ wording: "Could you clarify the basis of the quoted account?", evidence_ids: ["e"], group: "Morning foundation" });
  expect(JSON.stringify(request)).not.toContain("reviewer_id");
});

it.each(["Reject", "Defer"])("supports %s without implying approval", async (action) => {
  let current = view;
  const mock = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") { current = state(2, action === "Reject" ? "REJECTED" : "DEFERRED"); return json(current); }
    return json(url.endsWith("/history") ? [current.current] : current);
  }); vi.stubGlobal("fetch", mock);
  render(<DepositionWorkspace matterId="matter" threadId="thread" planId="plan" />);
  fireEvent.click(await screen.findByRole("button", { name: `${action} deposition plan` }));
  await waitFor(() => expect(posts(mock)).toHaveLength(1));
  expect(JSON.parse(posts(mock)[0][1].body).review.action).toBe(action.toUpperCase());
});

it("preserves the exact request across failure and remount; does not auto-resubmit", async () => {
  let sent = false; let current = view;
  const mock = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") { if (!sent) { sent = true; current = state(2); return json({}, 409); } return json(current); }
    return json(url.endsWith("/history") ? [current.current] : current);
  }); vi.stubGlobal("fetch", mock);
  const rendered = render(<DepositionWorkspace matterId="matter" threadId="thread" planId="plan" />);
  fireEvent.click(await screen.findByRole("button", { name: "Save draft revision" }));
  await screen.findByRole("button", { name: "Retry identical deposition request" });
  await waitFor(() => expect(screen.getByRole("button", { name: "Save draft revision" })).toBeDisabled());
  const body = posts(mock)[0][1].body;
  rendered.unmount(); render(<DepositionWorkspace matterId="matter" threadId="thread" planId="plan" />);
  const retry = await screen.findByRole("button", { name: "Retry identical deposition request" });
  await waitFor(() => expect(retry).toBeEnabled());
  expect(posts(mock)).toHaveLength(1); fireEvent.click(retry);
  await waitFor(() => expect(posts(mock)).toHaveLength(2));
  expect(posts(mock)[1][1].body).toBe(body);
});

it("does not overwrite unsaved wording when refresh discovers a newer revision", async () => {
  let current = view;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => json(url.endsWith("/history") ? [current.current] : current)));
  render(<DepositionWorkspace matterId="matter" threadId="thread" planId="plan" />);
  const input = await screen.findByLabelText("Question 1");
  fireEvent.change(input, { target: { value: "Unsaved human wording?" } });
  current = state(2); fireEvent.click(screen.getByRole("button", { name: "Refresh deposition state" }));
  await screen.findByText(/newer server revision exists/);
  expect(screen.getByLabelText("Question 1")).toHaveValue("Unsaved human wording?");
  expect(screen.getByRole("button", { name: "Save draft revision" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Reload server draft (discard local changes)" }));
  expect(screen.getByLabelText("Question 1")).toHaveValue(question.wording);
});

it("reads approved material through the approved revision endpoint", async () => {
  const current = state(3);
  const mock = vi.fn(async (url: string) => json(url.endsWith("/history") ? [state(2, "APPROVED").current, current.current] : url.endsWith("/approved/2") ? state(2, "APPROVED") : current)); vi.stubGlobal("fetch", mock);
  render(<DepositionWorkspace matterId="matter" threadId="thread" planId="plan" />);
  fireEvent.click(await screen.findByRole("button", { name: "Read approved revision 2" }));
  const material = await screen.findByRole("region", { name: "Approved deposition material" });
  expect(within(material).getByText(/Historical approval/)).toBeInTheDocument();
  expect(within(material).getByText(question.wording)).toBeInTheDocument();
  expect(mock.mock.calls.some(([url]) => url.endsWith(`${base}/plan/approved/2`))).toBe(true);
});

it.each([[401, "Authentication required"], [403, "operator cannot access"], [404, "unavailable"], [422, "review is invalid"], [503, "unavailable"]])("handles HTTP %i without leaking server details", async (status, text) => {
  vi.stubGlobal("fetch", vi.fn(async () => json({ detail: "SECRET /private/path" }, status as number)));
  render(<DepositionWorkspace matterId="matter" threadId="thread" planId="plan" />);
  expect(await screen.findByRole("alert")).toHaveTextContent(text as string);
  expect(screen.queryByText(/SECRET/)).not.toBeInTheDocument();
});
