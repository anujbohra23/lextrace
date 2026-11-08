"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { depositionPath, PlanRevision, PlanSummary, PlanView, Question, ReviewAction } from "@/lib/deposition";
import { InvestigationView, Json } from "@/lib/investigations";
import { InvestigationData } from "./InvestigationReview";

function feedback(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "Authentication required. Check the operator credential in this browser tab.";
    if (error.status === 403) return "This operator cannot access the workspace.";
    if (error.status === 404) return "The matter, investigation, plan or approved revision is unavailable.";
    if (error.status === 409) return "The source snapshot or plan revision changed. Refresh server state; your submitted request is retained and no different decision is sent automatically.";
    if (error.status === 422) return "The review is invalid. Preserve each question's related evidence and required account citations, and explicitly confirm approval.";
    if (error.status === 503) return "Deposition preparation is unavailable. Refresh server state or retry the identical saved request.";
  }
  return "The request could not be confirmed. Check server progress; retry only the identical saved request.";
}

export function DepositionWorkspace({ matterId, threadId, planId }: { matterId: string; threadId: string; planId?: string }) {
  const router = useRouter();
  const root = depositionPath(matterId, threadId);
  const base = root + "/deposition-plans";
  const path = planId ? `${base}/${encodeURIComponent(planId)}` : base;
  const key = `lextrace.depositionRequest:${matterId}:${threadId}:${planId ?? "create"}`;
  const [run, setRun] = useState<InvestigationView | null>(null);
  const [plans, setPlans] = useState<PlanSummary[]>([]);
  const [view, setView] = useState<PlanView | null>(null);
  const [history, setHistory] = useState<PlanRevision[]>([]);
  const [approved, setApproved] = useState<PlanView | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [valid, setValid] = useState(false);
  const [epoch, setEpoch] = useState(0);
  const dispatching = useRef(false);
  const refresh = useCallback(async () => {
    if (planId) {
      const [next, audit] = await Promise.all([api<PlanView>(path), api<PlanRevision[]>(path + "/history")]);
      setView(next); setHistory(audit); setValid(true);
    } else {
      const current = await api<InvestigationView>(root);
      setRun(current);
      setPlans(current.stage === "APPROVED" ? await api<PlanSummary[]>(base) : []);
      setValid(true);
    }
  }, [base, path, planId, root]);
  useEffect(() => {
    let active = true;
    void Promise.resolve().then(async () => {
      try { if (active) setPending(sessionStorage.getItem(key)); await refresh(); }
      catch (failure) { if (active) { setError(feedback(failure)); setValid(false); } }
    });
    return () => { active = false; };
  }, [key, refresh]);
  async function reload() {
    setBusy(true); setError(""); setApproved(null);
    try { await refresh(); } catch (failure) { setError(feedback(failure)); setValid(false); }
    finally { setBusy(false); }
  }
  async function transmit(body: string) {
    if (dispatching.current) return;
    dispatching.current = true; setBusy(true); setError("");
    try {
      const result = await api<PlanView>(planId ? path + "/review" : base, { method: "POST", body });
      sessionStorage.removeItem(key); setPending(null);
      if (!planId) router.push(`${root}/deposition/${result.basis.plan_id}`);
      else { await refresh(); setEpoch((n) => n + 1); setApproved(null); }
    } catch (failure) {
      setError(feedback(failure));
      try { await refresh(); } catch { setValid(false); }
    } finally { dispatching.current = false; setBusy(false); }
  }
  function send(body: object) {
    if (pending || dispatching.current) return;
    const frozen = JSON.stringify(body);
    try { sessionStorage.setItem(key, frozen); }
    catch { setError("The request was not sent because browser retry storage is unavailable."); return; }
    setPending(frozen); void transmit(frozen);
  }
  function discard() {
    if (!window.confirm("Discard this saved request? This does not undo any server decision. Local drafts will be reloaded from the server.")) return;
    try { sessionStorage.removeItem(key); } catch { setError("The saved request could not be removed."); return; }
    setPending(null); setEpoch((n) => n + 1); void reload();
  }
  async function readApproved(revision: number) {
    setBusy(true); setError(""); setApproved(null);
    try { setApproved(await api<PlanView>(`${path}/approved/${revision}`)); }
    catch (failure) { setError(feedback(failure)); setValid(false); }
    finally { setBusy(false); }
  }
  return <section className="investigation-workspace" aria-label="Deposition preparation">
    <div className="matter-heading"><div><p className="eyebrow">Matter · deposition preparation</p><h1>Witness preparation</h1></div><Link href={root}>Back to investigation →</Link></div>
    <p>Human-reviewed preparation material is separate from the signed M7 investigative hypotheses. Source attribution does not establish personal knowledge. Equal names do not establish equal identities.</p>
    {error && <p role="alert" className="notice">{error}</p>}
    <div className="review-actions"><button disabled={busy} onClick={() => void reload()}>Refresh deposition state</button>{planId && <Link href={`${root}/deposition`}>All deposition plans →</Link>}</div>
    {pending && <section className="notice" aria-label="Saved deposition request"><p>The exact submitted request is retained for safe retry. New decisions are locked.</p><button disabled={busy} onClick={() => void transmit(pending)}>Retry identical deposition request</button><button disabled={busy} onClick={discard}>Discard saved deposition request</button></section>}
    {!planId && <><h2>Source investigation</h2>{run && <p>{run.stage} · coverage {run.coverage.join(", ") || "Not reported"}</p>}{run?.stage !== "APPROVED" && <p className="notice">An approved, current M7 snapshot is required. Missing or quarantined extraction is not permission to invent evidence.</p>}
      <button disabled={!valid || busy || !!pending || !run?.snapshot_id || run.stage !== "APPROVED"} onClick={() => send({ snapshot_id: run?.snapshot_id, request_id: crypto.randomUUID() })}>Create deposition plan</button>
      <h2>Existing plans</h2><div className="matter-cards">{plans.map((item) => <Link key={item.plan_id} href={`${root}/deposition/${item.plan_id}`}><strong>Plan {item.plan_id.slice(0, 12)}</strong><span>{item.status} · revision {item.revision} · {item.outcome} · {item.witness_count} account-scoped witnesses</span></Link>)}</div></>}
    {view && <><h2>{view.current.status} · revision {view.current.revision}</h2><p>{view.basis.outcome} · coverage: {view.basis.coverage.join(", ") || "Not reported"}</p>{view.basis.limitations.length > 0 && <p className="notice">Limitations: {view.basis.limitations.join(", ")}</p>}<p className="evidence-meta">Source snapshot {view.basis.snapshot_id} · artifact {view.basis.artifact_digest}</p>
      <WitnessDossiers view={view} />
      <PlanEditor key={`${view.basis.plan_id}:${epoch}`} view={view} disabled={!valid || busy || !!pending} onSubmit={(review) => send({ review })} />
      <h3>Durable review history</h3><ul>{history.map((item) => <li key={item.revision}>Revision {item.revision} · {item.action} · {item.status} · {item.reviewer_id} · {item.timestamp}{item.status === "APPROVED" && <button disabled={busy} onClick={() => void readApproved(item.revision)}>Read approved revision {item.revision}</button>}</li>)}</ul>
    </>}
    {approved && <section aria-label="Approved deposition material" className="evidence-card"><h2>Human-approved deposition plan · revision {approved.current.revision}</h2>{view && view.current.revision > approved.current.revision && <p className="notice">Historical approval; the current plan has a later revision.</p>}<p>This does not modify the M7 snapshot or prove an allegation.</p>{approved.current.questions.map((q) => <article key={q.question_id}><h3>{q.category.replaceAll("_", " ")}</h3><p>{q.wording}</p><p>{q.rationale}</p><QuestionEvidence view={approved} question={q} /></article>)}</section>}
  </section>;
}

function WitnessDossiers({ view }: { view: PlanView }) {
  return <section aria-label="Witness dossiers"><h3>Witness dossiers · source accounts remain separate</h3>{view.basis.witnesses.length === 0 && <p>No attributable reviewed witness evidence is available.</p>}{view.basis.witnesses.map((witness) => <article key={witness.witness_id} className="evidence-card"><h4>{witness.label}</h4><p className="evidence-meta">Account-scoped witness {witness.witness_id}</p><p>{witness.uncertainties.join(" · ")}</p><details><summary>Reviewed source accounts and verbatim evidence</summary><InvestigationData value={witness.accounts as unknown as Json} /></details><details><summary>Account timing and related participant chronology</summary><p>Related participant context is not attributed witness testimony.</p><InvestigationData value={witness.chronology} /></details><details><summary>Related potential inconsistencies · unverified hypotheses</summary><InvestigationData value={witness.findings} /></details><details><summary>Reviewed participant links · not speaker identity merges</summary><InvestigationData value={witness.related_participants} /></details></article>)}</section>;
}

function QuestionEvidence({ view, question }: { view: PlanView; question: Question }) {
  return <>{question.evidence_ids.map((id) => {
    const item = view.basis.evidence.find((e) => e.evidence_id === id);
    if (!item) return <p key={id} role="alert">Evidence unavailable; do not rely on this question.</p>;
    const ref = item.reference;
    return <div key={id}><blockquote>{ref.exact_quote}</blockquote><p className="evidence-meta">Document {ref.document_id} · version {ref.version_id} · section {ref.section_id} · characters {ref.start}–{ref.end}{ref.page_start ? ` · pages ${ref.page_start}–${ref.page_end}` : ""}</p></div>;
  })}</>;
}

function PlanEditor({ view, disabled, onSubmit }: { view: PlanView; disabled: boolean; onSubmit: (review: object) => void }) {
  const [questions, setQuestions] = useState(view.current.questions);
  const [revision, setRevision] = useState(view.current.revision);
  const [confirmed, setConfirmed] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const stale = revision !== view.current.revision;
  const blocked = view.basis.outcome === "BLOCKED";
  function update(id: string, values: Partial<Question>) { setQuestions(questions.map((q) => q.question_id === id ? { ...q, ...values } : q)); }
  function submit(action: ReviewAction) {
    if (disabled || stale) return;
    if (action === "APPROVE" && (!confirmed || blocked || (view.basis.outcome === "LIMITED" && !acknowledged))) return;
    onSubmit({ decision_id: crypto.randomUUID(), expected_revision: revision, action, questions,
      confirm: confirmed, acknowledge_limitations: acknowledged });
  }
  function replaceDraft() { setQuestions(view.current.questions); setRevision(view.current.revision); setConfirmed(false); setAcknowledged(false); }
  return <section aria-label="Question review" className="investigation-review"><h3>Question drafts · {view.current.status}</h3><p>Edited wording and rationale are human-authored interpretations. Review every factual premise against the cited quotations before approval. Source matching alone does not establish semantic truth.</p>{stale && <p role="alert" className="notice">A newer server revision exists. Your local draft has not been overwritten.</p>}<button disabled={disabled} onClick={replaceDraft}>Reload server draft (discard local changes)</button>
    <fieldset disabled={disabled || stale}>{questions.map((q, i) => <article key={q.question_id} className="evidence-card"><h4>{q.category.replaceAll("_", " ")}</h4><p>{view.basis.witnesses.find((w) => w.witness_id === q.witness_id)?.label} · account {q.witness_id.slice(0, 12)}</p><label>Question {i + 1}<textarea maxLength={2000} value={q.wording} onChange={(e) => update(q.question_id, { wording: e.target.value })} /></label><label>Rationale {i + 1}<textarea maxLength={2000} value={q.rationale} onChange={(e) => update(q.question_id, { rationale: e.target.value })} /></label><div className="matrix-controls"><label>Order {i + 1}<input type="number" min={0} max={1000} value={q.order} onChange={(e) => update(q.question_id, { order: Number(e.target.value) })} /></label><label>Optional preparation group {i + 1}<input maxLength={120} value={q.group} onChange={(e) => update(q.question_id, { group: e.target.value })} /></label><label>Disposition {i + 1}<select value={q.disposition} onChange={(e) => update(q.question_id, { disposition: e.target.value as Question["disposition"] })}><option value="INCLUDE">Include</option><option value="REJECT">Reject question</option><option value="DEFER">Defer question</option></select></label></div>
      <p>Evidence selection stays within this question&apos;s original accounts. Potential inconsistency questions require evidence from both accounts.</p>{view.basis.questions.find((original) => original.question_id === q.question_id)?.evidence_ids.map((id) => <label className="investigation-check" key={id}><input type="checkbox" aria-label={`Evidence ${i + 1} ${id}`} checked={q.evidence_ids.includes(id)} onChange={(e) => update(q.question_id, { evidence_ids: e.target.checked ? [...q.evidence_ids, id] : q.evidence_ids.filter((v) => v !== id) })} />{view.basis.evidence.find((e) => e.evidence_id === id)?.reference.exact_quote}</label>)}<QuestionEvidence view={view} question={q} /></article>)}
      {blocked && <p className="notice">Plan blocked: there is no sufficient attributable reviewed evidence to approve.</p>}
      {view.basis.outcome === "LIMITED" && <label className="investigation-check"><input type="checkbox" checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} />I acknowledge the partial coverage, quarantined evidence and other listed limitations.</label>}
      <label className="investigation-check"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />I confirm approval of this exact deposition plan revision and its source-grounded wording.</label>
      <div className="review-actions"><button onClick={() => submit("REVISE")}>Save draft revision</button><button disabled={blocked || !confirmed || (view.basis.outcome === "LIMITED" && !acknowledged)} onClick={() => submit("APPROVE")}>Approve deposition plan</button><button onClick={() => submit("REJECT")}>Reject deposition plan</button><button onClick={() => submit("DEFER")}>Defer deposition plan</button></div>
    </fieldset>
  </section>;
}
