import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { InvestigationWorkspace } from "./InvestigationWorkspace";
import { decisionStorageKey, InvestigationView, ReviewRecord } from "@/lib/investigations";

const { push } = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

const base = "/matters/matter/investigations";
const runPath = `${base}/thread`;
const source = { matter_id: "matter", document_id: "document", version_id: "active-version", run_id: "extraction" };
const assertion: ReviewRecord = { original: { ...source, assertion_id: "account", normalized_proposition: "Delivered on Monday", evidence_references: [{ ...source, section_id: "section", start: 4, end: 24, exact_quote: "The source account." }] }, revision: 2, review_state: "PROPOSED" };
const mention: ReviewRecord = { original: { ...source, mention_id: "mention", mention_text: "Morgan", evidence_references: [{ ...source, section_id: "section", start: 25, end: 31, exact_quote: "Morgan" }] }, revision: 1, review_state: "PROPOSED" };
const evidence: InvestigationView = { matter_id: "matter", thread_id: "thread", run_id: "run", stage: "WAITING_REVIEW", errors: [], coverage: ["COMPLETE"], checkpoint: "evidence", token: "evidence-token", digest: "a".repeat(64), review: { records: [assertion, mention], chronology: { ordering_policy: "inclusive-overlap-components-v1", unresolved_events: [] }, findings: null }, snapshot_id: null };
const findings: InvestigationView = { ...evidence, checkpoint: "findings", token: "findings-token", digest: "b".repeat(64), review: { records: [assertion, mention], chronology: { ordering_policy: "inclusive-overlap-components-v1" }, findings: { findings: [{ finding_id: "finding", status: "POTENTIAL_INCONSISTENCY", verification: "INDEPENDENT_STRUCTURED_SUPPORT", accounts: [{ reference: { exact_quote: "Finding source quote." } }] }] } } };
const snapshot = { snapshot_id: "signed-snapshot", matter_id: "matter", thread_id: "thread", run_id: "run", reviewer_id: "operator", meaning: "HUMAN_SIGNED_INVESTIGATION_HYPOTHESES", chronology_id: "chronology", finding_ids: ["finding"] };
const approved: InvestigationView = { ...findings, stage: "APPROVED", checkpoint: null, token: null, digest: null, review: null, snapshot_id: "signed-snapshot" };
function json(value: unknown, status = 200) { return { ok: status >= 200 && status < 300, status, json: async () => value } as Response; }
function confirm(gate: "evidence" | "findings") {
  fireEvent.click(screen.getByRole("checkbox", { name: new RegExp(`I confirm approval of this ${gate} review`) }));
}
function posts(fetchMock: ReturnType<typeof vi.fn>) {
  return fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
}

beforeEach(() => { sessionStorage.clear(); push.mockClear(); sessionStorage.setItem("lextrace.operatorToken", "tab-credential"); });
afterEach(() => { cleanup(); sessionStorage.clear(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("lists matter-scoped runs and disables starting when no versions are eligible", async () => {
  const fetchMock = vi.fn(async (url: string) => json(url.endsWith("/eligible-versions") ? [] : [evidence])); vi.stubGlobal("fetch", fetchMock);
  render(<InvestigationWorkspace matterId="matter" />);
  expect(await screen.findByRole("link", { name: /Run run/ })).toHaveAttribute("href", runPath);
  expect(screen.getByRole("link", { name: "Back to matter →" })).toHaveAttribute("href", "/matters/matter");
  expect(screen.getByText(/No eligible document versions are available/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Start investigation" })).toBeDisabled();
  expect(posts(fetchMock)).toHaveLength(0);
});

it("fetches eligible versions, starts with the selected exact pair and authenticates", async () => {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => json(url.endsWith("/eligible-versions") ? [{ document_id: "document", version_id: "active-version" }] : init?.method === "POST" ? evidence : []));
  vi.stubGlobal("fetch", fetchMock);
  render(<InvestigationWorkspace matterId="matter" />);
  await screen.findByText("No investigations are available.");
  fireEvent.click(screen.getByRole("checkbox", { name: "Document document · version active-version" }));
  fireEvent.click(screen.getByRole("button", { name: "Start investigation" }));
  await waitFor(() => expect(push).toHaveBeenCalledWith(runPath));
  expect(JSON.parse(posts(fetchMock)[0][1].body)).toEqual({ approved_versions: [{ document_id: "document", version_id: "active-version" }] });
  for (const [url, init] of fetchMock.mock.calls) {
    expect(url).not.toContain("tab-credential");
    expect(new Headers(init?.headers).get("authorization")).toBe("Bearer tab-credential");
    expect(init?.cache).toBe("no-store");
  }
});

it("reviews evidence with exact provenance, record commands and entity links", async () => {
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => json(init?.method === "POST" ? findings : { ...evidence, coverage: ["PARTIAL"] }));
  vi.stubGlobal("fetch", fetchMock);
  render(<InvestigationWorkspace matterId="matter" threadId="thread" />);
  expect(await screen.findByText("The source account.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Download investigation report" })).toBeDisabled();
  expect(screen.getByText(/characters 4–24/)).toBeInTheDocument();
  const approve = screen.getByRole("button", { name: "Approve evidence review" });
  expect(approve).toBeDisabled();
  confirm("evidence"); expect(approve).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: /I acknowledge incomplete coverage/ }));
  fireEvent.change(screen.getByLabelText("Decision for account"), { target: { value: "ACCEPT" } });
  fireEvent.change(screen.getByLabelText("Explanation for account"), { target: { value: "Checked exact source" } });
  fireEvent.click(within(screen.getByRole("group", { name: "Entity mentions linked to account" })).getByRole("checkbox", { name: /Morgan/ }));
  fireEvent.change(screen.getByLabelText("Decision for mention"), { target: { value: "EDIT" } });
  fireEvent.change(screen.getByLabelText("Resolved entity for mention"), { target: { value: "reviewed-person" } });
  fireEvent.click(approve);
  await screen.findByRole("button", { name: "Approve findings review" });
  const body = JSON.parse(posts(fetchMock)[0][1].body);
  expect(body).toMatchObject({ run_id: "run", gate: "evidence", token: "evidence-token", digest: evidence.digest, decision: "APPROVE", acknowledge_incomplete_coverage: true });
  expect(body.evidence_decisions).toHaveLength(2);
  expect(body.evidence_decisions[0]).toMatchObject({ expected_revision: 2, target: { ...source, kind: "assertion", record_id: "account" }, explanation: "Checked exact source" });
  expect(body.evidence_decisions[0].decision_id).toBeTruthy();
  expect(body.evidence_decisions[0].timestamp).toMatch(/Z$/);
  expect(body.evidence_decisions[1]).toMatchObject({ decision: "EDIT", resolved_entity_id: "reviewed-person" });
  expect(body.entity_links).toEqual([{ account: { ...source, kind: "assertion", record_id: "account" }, mentions: [{ ...source, kind: "mention", record_id: "mention" }] }]);
  expect(JSON.stringify(body)).not.toContain("reviewer_id");
  expect(screen.getByRole("button", { name: "Approve findings review" })).toBeDisabled();
});

it("supports valid date edits and stops incomplete edits before submission", async () => {
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => json(init?.method === "POST" ? findings : evidence)); vi.stubGlobal("fetch", fetchMock);
  render(<InvestigationWorkspace matterId="matter" threadId="thread" />);
  await screen.findByLabelText("Decision for account");
  fireEvent.change(screen.getByLabelText("Decision for account"), { target: { value: "EDIT" } });
  confirm("evidence"); fireEvent.click(screen.getByRole("button", { name: "Approve evidence review" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Choose a date correction");
  expect(posts(fetchMock)).toHaveLength(0);
  fireEvent.change(screen.getByLabelText("Date correction for account"), { target: { value: "exact" } });
  fireEvent.change(screen.getByLabelText("Start date for account"), { target: { value: "2020-01-02" } });
  fireEvent.click(screen.getByRole("button", { name: "Approve evidence review" }));
  await waitFor(() => expect(posts(fetchMock)).toHaveLength(1));
  expect(JSON.parse(posts(fetchMock)[0][1].body).evidence_decisions[0].date_correction).toEqual({ kind: "exact", earliest: "2020-01-02", latest: "2020-01-02", precision: "day" });
});

it("requires findings confirmation and coverage acknowledgement without evidence edits", async () => {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => json(url.includes("/snapshots/") ? snapshot : init?.method === "POST" ? approved : { ...findings, coverage: ["OCR_REQUIRED"] })); vi.stubGlobal("fetch", fetchMock);
  render(<InvestigationWorkspace matterId="matter" threadId="thread" />);
  await screen.findByText("Finding source quote.");
  expect(screen.queryByLabelText("Decision for account")).not.toBeInTheDocument();
  const button = screen.getByRole("button", { name: "Approve findings review" });
  confirm("findings"); expect(button).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: /I acknowledge incomplete coverage/ }));
  fireEvent.click(button);
  await waitFor(() => expect(posts(fetchMock)).toHaveLength(1));
  expect(JSON.parse(posts(fetchMock)[0][1].body)).toEqual({ run_id: "run", gate: "findings", token: "findings-token", digest: findings.digest, decision: "APPROVE", acknowledge_incomplete_coverage: true, entity_links: [], evidence_decisions: [] });
});

it.each([evidence, findings])("rejects the $checkpoint gate without requiring approval confirmation", async (view) => {
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => json(init?.method === "POST" ? { ...view, stage: "BLOCKED", errors: ["REJECTED"], checkpoint: null, token: null, digest: null, review: null } : view)); vi.stubGlobal("fetch", fetchMock);
  render(<InvestigationWorkspace matterId="matter" threadId="thread" />);
  vi.spyOn(window, "confirm").mockReturnValue(true);
  fireEvent.click(await screen.findByRole("button", { name: "Reject investigation" }));
  await screen.findByText("Run diagnostics: REJECTED");
  expect(JSON.parse(posts(fetchMock)[0][1].body)).toMatchObject({ gate: view.checkpoint, decision: "REJECT", token: view.token, digest: view.digest, evidence_decisions: [], entity_links: [] });
});

it("retains the byte-identical request across failure, stale refresh and page remount", async () => {
  let submitted = false; let recovered = false;
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "POST") { if (!submitted) { submitted = true; return json({}, 503); } recovered = true; return json(findings); }
    return json(submitted ? findings : evidence);
  }); vi.stubGlobal("fetch", fetchMock);
  const first = render(<InvestigationWorkspace matterId="matter" threadId="thread" />);
  await screen.findByLabelText("Decision for account");
  fireEvent.change(screen.getByLabelText("Decision for account"), { target: { value: "DEFER" } });
  confirm("evidence"); fireEvent.click(screen.getByRole("button", { name: "Approve evidence review" }));
  await screen.findByRole("button", { name: "Retry identical review request" });
  await screen.findByRole("button", { name: "Approve findings review" });
  const originalBody = posts(fetchMock)[0][1].body;
  expect(sessionStorage.getItem(decisionStorageKey("matter", "thread"))).toBe(originalBody);
  expect(screen.getByRole("button", { name: "Approve findings review" })).toBeDisabled();
  first.unmount();
  render(<InvestigationWorkspace matterId="matter" threadId="thread" />);
  const retry = await screen.findByRole("button", { name: "Retry identical review request" });
  await waitFor(() => expect(retry).toBeEnabled());
  expect(posts(fetchMock)).toHaveLength(1);
  fireEvent.click(retry);
  await waitFor(() => expect(recovered).toBe(true));
  expect(posts(fetchMock)[1][1].body).toBe(originalBody);
  await waitFor(() => expect(screen.queryByRole("button", { name: "Retry identical review request" })).not.toBeInTheDocument());
  expect(sessionStorage.getItem(decisionStorageKey("matter", "thread"))).toBeNull();
});

it("refreshes on 409 without automatically resubmitting a different decision", async () => {
  let conflict = false;
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => { if (init?.method === "POST") { conflict = true; return json({}, 409); } return json(conflict ? findings : evidence); }); vi.stubGlobal("fetch", fetchMock);
  render(<InvestigationWorkspace matterId="matter" threadId="thread" />);
  await screen.findByRole("button", { name: "Approve evidence review" }); confirm("evidence");
  fireEvent.click(screen.getByRole("button", { name: "Approve evidence review" }));
  await screen.findByRole("button", { name: "Approve findings review" });
  expect(screen.getByRole("alert")).toHaveTextContent("no new decision is submitted automatically");
  expect(posts(fetchMock)).toHaveLength(1);
  expect(screen.getByRole("button", { name: "Approve findings review" })).toBeDisabled();
});

it("refreshes server progress and resets approval confirmation at the new gate", async () => {
  let current = evidence;
  const fetchMock = vi.fn(async () => json(current)); vi.stubGlobal("fetch", fetchMock);
  render(<InvestigationWorkspace matterId="matter" threadId="thread" />);
  await screen.findByRole("button", { name: "Approve evidence review" }); confirm("evidence");
  current = findings; fireEvent.click(screen.getByRole("button", { name: "Refresh server status" }));
  expect(await screen.findByRole("button", { name: "Approve findings review" })).toBeDisabled();
  expect(posts(fetchMock)).toHaveLength(0);
});

it.each([
  [401, "Authentication required"], [403, "operator cannot access"],
  [422, "review request is invalid"], [503, "service is unavailable"],
])("explains HTTP %i failures without exposing server details", async (status, message) => {
  vi.stubGlobal("fetch", vi.fn(async () => json({ detail: "PRIVATE secret path" }, status as number)));
  render(<InvestigationWorkspace matterId="matter" threadId="thread" />);
  expect(await screen.findByRole("alert")).toHaveTextContent(message as string);
  expect(screen.queryByText(/PRIVATE/)).not.toBeInTheDocument();
});

it("reads published metadata and signed artifacts only through scoped authenticated endpoints", async () => {
  const signedPath = `${runPath}/snapshots/signed-snapshot`;
  const fetchMock = vi.fn(async (url: string) => {
    if (url.endsWith("/artifacts/chronology")) return json({ ordering_policy: "Signed chronology policy" });
    if (url.endsWith("/artifacts/finding")) return json({ accounts: [{ reference: { exact_quote: "Signed source passage" } }] });
    if (url.endsWith("/snapshots/signed-snapshot")) return json({ snapshot_id: "signed-snapshot", reviewer_id: "operator", meaning: "HUMAN_SIGNED_INVESTIGATION_HYPOTHESES", chronology_id: "chronology", finding_ids: ["finding"] });
    return json(approved);
  }); vi.stubGlobal("fetch", fetchMock);
  render(<InvestigationWorkspace matterId="matter" threadId="thread" />);
  await screen.findByText("Signed by operator");
  expect(screen.getByRole("button", { name: "Download investigation report" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "Read signed chronology" }));
  await screen.findByText("Signed chronology policy");
  fireEvent.click(screen.getByRole("button", { name: "Read signed finding 1" }));
  await screen.findByText("Signed source passage");
  expect(screen.queryByText("Signed chronology policy")).not.toBeInTheDocument();
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(expect.arrayContaining([expect.stringContaining(signedPath), expect.stringContaining(`${signedPath}/artifacts/chronology`), expect.stringContaining(`${signedPath}/artifacts/finding`)]));
  for (const [url, init] of fetchMock.mock.calls as unknown as [string, RequestInit & { headers: Record<string, string> }][]) {
    expect(init.headers.Authorization).toBe("Bearer tab-credential"); expect(url).not.toContain("tab-credential");
  }
});

it("locks review actions when a refresh can no longer validate server state", async () => {
  let stale = false;
  vi.stubGlobal("fetch", vi.fn(async () => stale ? json({}, 409) : json(evidence)));
  render(<InvestigationWorkspace matterId="matter" threadId="thread" />);
  await screen.findByRole("button", { name: "Approve evidence review" }); confirm("evidence");
  stale = true; fireEvent.click(screen.getByRole("button", { name: "Refresh server status" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "Approve evidence review" })).not.toBeInTheDocument();
});

it.each([409, 422])("refreshes eligible versions after a rejected start (%i) without retrying", async (status) => {
  let stale = false;
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") { stale = true; return json({}, status); }
    return json(url.endsWith("/eligible-versions")
      ? [{ document_id: "document", version_id: stale ? "replacement-version" : "active-version" }]
      : []);
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<InvestigationWorkspace matterId="matter" />);
  fireEvent.click(await screen.findByRole("checkbox", { name: /version active-version/ }));
  fireEvent.click(screen.getByRole("button", { name: "Start investigation" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(status === 409 ? "stale or invalid" : "rejected as invalid");
  const replacement = await screen.findByRole("checkbox", { name: /version replacement-version/ });
  expect(replacement).not.toBeChecked();
  expect(screen.queryByRole("checkbox", { name: /version active-version/ })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Start investigation" })).toBeDisabled();
  expect(posts(fetchMock)).toHaveLength(1);
  expect(push).not.toHaveBeenCalled();
});

it("does not offer sources for a missing matter", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => json({}, 404)));
  render(<InvestigationWorkspace matterId="missing" />);
  expect(await screen.findByRole("alert")).toHaveTextContent("unavailable in this matter");
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Start investigation" })).toBeDisabled();
});
