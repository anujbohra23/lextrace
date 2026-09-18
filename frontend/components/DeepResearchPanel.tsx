"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Attack, Coverage, Plan, ResearchRun, ResearchStep } from "@/lib/deepResearch";

export function DeepResearchPanel({ matterId, claimId, onUpdate }: { matterId: string; claimId: string; onUpdate: () => void }) {
  const base = `/matters/${matterId}/claims/${claimId}`;
  const [coverage, setCoverage] = useState<Coverage | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [run, setRun] = useState<ResearchRun | null>(null);
  const [attacks, setAttacks] = useState<Attack[]>([]);
  const [redTeamJob, setRedTeamJob] = useState<string | null>(null);
  const [error, setError] = useState("");
  const refresh = useCallback(async () => {
    try {
      const [nextCoverage, nextAttacks] = await Promise.all([
        api<Coverage>(`${base}/research-coverage`), api<Attack[]>(`${base}/attacks`),
      ]);
      if (!Array.isArray(nextCoverage?.gaps) || !Array.isArray(nextAttacks)) throw new Error("Invalid research details.");
      setCoverage(nextCoverage); setAttacks(nextAttacks); setError("");
      try { setPlan(await api<Plan>(`${base}/research-plan`)); } catch { setPlan(null); }
    } catch { setError("Research details could not be loaded. Close this claim and reopen it to retry."); }
  }, [base]);
  useEffect(() => { void Promise.resolve().then(refresh); }, [refresh]);
  useEffect(() => {
    if (!run || !["queued", "running"].includes(run.status)) return;
    const timer = window.setInterval(() => {
      void api<ResearchRun>(`/matters/${matterId}/research-runs/${run.run_id}`)
        .then((next) => { setRun(next); if (!["queued", "running"].includes(next.status)) { void refresh(); onUpdate(); } })
        .catch(() => setError("Research status could not be loaded."));
    }, 1500);
    return () => window.clearInterval(timer);
  }, [matterId, onUpdate, refresh, run]);
  useEffect(() => {
    if (!redTeamJob) return;
    const timer = window.setInterval(() => {
      void api<{ status: string }>(`/matters/${matterId}/jobs/${redTeamJob}`)
        .then((job) => { if (job.status === "completed" || job.status === "failed") { setRedTeamJob(null); void refresh(); onUpdate(); } })
        .catch(() => setError("Red Team status could not be loaded."));
    }, 1500);
    return () => window.clearInterval(timer);
  }, [matterId, onUpdate, redTeamJob, refresh]);

  async function createPlan() {
    try { setPlan(await api<Plan>(`${base}/research-plan`, { method: "POST" })); }
    catch { setError("Research plan could not be created."); }
  }
  async function savePlan(steps: ResearchStep[], approve: boolean) {
    try { setPlan(await api<Plan>(`${base}/research-plan`, { method: "PATCH", body: JSON.stringify({ steps, approve }) })); }
    catch { setError("Research plan could not be saved."); }
  }
  function addStep() {
    if (!plan || !coverage?.gaps.length) return;
    const gap = coverage.gaps[0];
    const step: ResearchStep = {
      step_id: crypto.randomUUID(), claim_id: claimId, gap_id: gap.gap_id,
      intent: "DIRECT_SUPPORT", query: gap.objective, objective: gap.objective,
      retrieval_strategy: "reranked", max_results: 5, priority: gap.priority,
      approved: false,
    };
    setPlan({ ...plan, status: "DRAFT", steps: [...plan.steps, step] });
  }
  async function start() {
    try {
      const accepted = await api<{ run_id: string }>(`${base}/deep-research`, { method: "POST" });
      setRun({ run_id: accepted.run_id, status: "queued", stop_reason: null, warning: null, rounds: [], coverage: null });
    } catch { setError("Deep Research could not start. Approve a current plan first."); }
  }
  async function stop() {
    if (!run) return;
    try { setRun(await api<ResearchRun>(`/matters/${matterId}/research-runs/${run.run_id}/stop`, { method: "POST" })); }
    catch { setError("Research could not be stopped."); }
  }
  async function redTeam() {
    try { const accepted = await api<{ job_id: string }>(`${base}/red-team`, { method: "POST" }); setRedTeamJob(accepted.job_id); }
    catch { setError("Red Team analysis could not start."); }
  }
  return <section className="precedent-panel" aria-label="Deep Research and Red Team">
    <h3>Research coverage</h3><p>Coverage describes the indexed corpus, not all relevant law.</p>
    {error && <p role="alert">{error}</p>}
    {coverage && <><strong>{coverage.category}</strong><p>{coverage.warning}</p><p>{coverage.queries_executed} queries · {coverage.authorities_retrieved} authorities recorded</p>
      <h4>Research gaps</h4><ul>{coverage.gaps.map((gap) => <li key={gap.gap_id}><strong>{gap.type.replaceAll("_", " ")}</strong> · {gap.explanation}</li>)}</ul></>}
    {!plan ? <button onClick={() => void createPlan()}>Create research plan</button> : <><h4>Review research plan · {plan.status}</h4>
      {plan.steps.map((step) => <article className="precedent-card" key={step.step_id}><label><input type="checkbox" checked={step.approved} onChange={(event) => setPlan({ ...plan, status: "DRAFT", steps: plan.steps.map((item) => item.step_id === step.step_id ? { ...item, approved: event.target.checked } : item) })} /> {step.intent.replaceAll("_", " ")}</label><input aria-label={`Query ${step.step_id}`} value={step.query} onChange={(event) => setPlan({ ...plan, status: "DRAFT", steps: plan.steps.map((item) => item.step_id === step.step_id ? { ...item, query: event.target.value } : item) })} /><button onClick={() => setPlan({ ...plan, status: "DRAFT", steps: plan.steps.filter((item) => item.step_id !== step.step_id) })}>Remove step</button></article>)}
      <div className="review-actions"><button disabled={plan.steps.length >= 12 || !coverage?.gaps.length} onClick={addStep}>Add step</button><button onClick={() => void savePlan(plan.steps, true)}>Approve plan</button><button disabled={plan.status !== "APPROVED"} onClick={() => void start()}>Investigate this claim</button></div></>}
    {run && <><h4>Research history · {run.status}</h4>{run.rounds.map((round) => <p key={round.number}>Round {round.number}: {round.queries.length} queries · {round.new_case_ids.length} new cases · {round.resolved_gap_ids.length} gaps resolved</p>)}{run.warning && <p className="notice">{run.warning}</p>}{run.coverage && <p>Final coverage: {run.coverage.category}</p>}{["queued", "running"].includes(run.status) && <button onClick={() => void stop()}>Stop research</button>}</>}
    <h3>Counterarguments</h3><p>Only verified findings appear on the Matter attack surface. Severity does not predict the outcome.</p><button disabled={!!redTeamJob} onClick={() => void redTeam()}>Find counterarguments</button>{redTeamJob && <p role="status">Red Team running…</p>}
    {attacks.map((attack) => <details key={attack.attack_id}><summary>{attack.severity} · {attack.attack_type.replaceAll("_", " ")}{attack.verified ? "" : " · unverified"}</summary><p>{attack.explanation}</p><p>Cases: {attack.authority_case_ids.join(", ") || "None"} · Passages: {attack.passage_ids.join(", ") || "None"}</p>{attack.matter_spans?.map((span) => <p key={`${span.section_id}-${span.start}`}>Matter document {span.document_id} · characters {span.start}–{span.end}</p>)}<p>Next step: {attack.remediation}</p>{attack.verification_notes.map((note) => <p key={note}>{note}</p>)}</details>)}
  </section>;
}
