"use client";

import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { AlertDetail, MatterAlert, MonitoringOverview, MonitoringRun } from "@/lib/monitoring";

const severities = ["ALL", "CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL"];

export function MonitoringPanel({ matterId, onUpdate, openClaim }: {
  matterId: string; onUpdate: () => void; openClaim: (claimId: string) => void;
}) {
  const [overview, setOverview] = useState<MonitoringOverview | null>(null);
  const [alerts, setAlerts] = useState<MatterAlert[]>([]);
  const [selected, setSelected] = useState<AlertDetail | null>(null);
  const [severity, setSeverity] = useState("ALL");
  const [state, setState] = useState("ALL");
  const [claim, setClaim] = useState("ALL");
  const [run, setRun] = useState<MonitoringRun | null>(null);
  const [corpusPath, setCorpusPath] = useState("");
  const [useImpactModel, setUseImpactModel] = useState(false);
  const [error, setError] = useState("");

  async function refresh() {
    const [nextOverview, nextAlerts] = await Promise.all([
      api<MonitoringOverview>(`/matters/${matterId}/monitoring`),
      api<MatterAlert[]>(`/matters/${matterId}/alerts`),
    ]);
    setOverview(nextOverview); setAlerts(nextAlerts); onUpdate();
  }
  useEffect(() => { void Promise.resolve().then(refresh).catch(() => setError("Monitoring could not be loaded.")); }, [matterId]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!run || run.status === "completed" || run.status === "failed") return;
    const timer = window.setInterval(() => {
      void api<MonitoringRun>(`/monitoring/runs/${run.run_id}`).then((next) => {
        setRun(next);
        if (next.status === "completed") void refresh();
      }).catch(() => setError("Monitoring status could not be loaded."));
    }, 1500);
    return () => window.clearInterval(timer);
  }, [run, matterId]); // eslint-disable-line react-hooks/exhaustive-deps

  const visible = useMemo(() => alerts.filter((item) =>
    (severity === "ALL" || item.severity === severity) &&
    (state === "ALL" || item.review_state === state) &&
    (claim === "ALL" || item.claim_id === claim)
  ), [alerts, severity, state, claim]);

  async function review(alertId: string, reviewState: string) {
    try {
      await api(`/matters/${matterId}/alerts/${alertId}`, {
        method: "PATCH", body: JSON.stringify({ state: reviewState }),
      });
      await refresh();
      setSelected(await api<AlertDetail>(`/matters/${matterId}/alerts/${alertId}`));
    } catch { setError("Alert review could not be saved."); }
  }
  async function apply(alertId: string) {
    try {
      await api(`/matters/${matterId}/alerts/${alertId}/apply`, { method: "POST" });
      await refresh();
      setSelected(await api<AlertDetail>(`/matters/${matterId}/alerts/${alertId}`));
    } catch { setError("Alert could not be applied. Check its evidence and review state."); }
  }
  async function startRun() {
    try {
      const accepted = await api<{ run_id: string }>("/monitoring/run", {
        method: "POST", body: JSON.stringify({
          new_corpus_path: corpusPath, matter_ids: [matterId],
          limits: useImpactModel ? { max_llm_calls: 2, max_tokens: 5000 } : undefined,
        }),
      });
      setRun({ run_id: accepted.run_id, status: "queued", outcome: null, completed_at: null,
        new_cases_examined: 0, alerts_created: 0, targets_examined: 0, errors: [] });
      setError("");
    } catch { setError("Monitoring could not start. Select an existing backend data/ JSONL snapshot."); }
  }

  return <section className="workspace" aria-label="Matter monitoring">
    <h2>Living Matter alerts</h2>
    <p>Alerts describe changes in the currently indexed corpus. They do not predict an outcome or automatically change your argument.</p>
    {error && <p role="alert" className="notice">{error}</p>}
    <div className="matter-cards">
      <article><h3>Watching</h3><p>{overview?.targets.filter((item) => item.enabled).length ?? 0} targets · {overview?.unread_alert_count ?? 0} unread alerts</p>
        {overview?.targets.map((target) => <p key={target.target_id}>{target.target_type} {target.target_reference_id} · {target.enabled ? "Watching" : "Paused"} {target.automatic ? "(automatic)" : ""}</p>)}
      </article>
      <article><h3>Check a new corpus snapshot</h3><p>Point to a prepared JSONL file under backend data/. Index and graph rebuilds are separate explicit steps.</p>
        <input aria-label="New corpus path" value={corpusPath} placeholder="data/updated_cases.jsonl" onChange={(event) => setCorpusPath(event.target.value)} />
        <label><input type="checkbox" checked={useImpactModel} onChange={(event) => setUseImpactModel(event.target.checked)} /> Use configured impact model for bounded Stage 2 review</label>
        <button disabled={!corpusPath || run?.status === "running" || run?.status === "queued"} onClick={() => void startRun()}>Run monitoring</button>
        {run && <p role="status">{run.status} · {run.outcome ?? "checking"} · {run.new_cases_examined} new cases · {run.alerts_created} new alerts</p>}
      </article>
    </div>
    <h3>Monitoring history</h3>
    {overview?.recent_runs.length ? overview.recent_runs.map((item) => <p key={item.run_id}>
      {item.completed_at?.slice(0, 10) ?? "In progress"} · {item.new_cases_examined} new cases checked · {item.alerts_created} alerts · {item.outcome ?? item.status}
    </p>) : <p>No monitoring runs yet. No change is a valid result.</p>}
    <h3>Alert center</h3>
    <div className="matrix-controls"><label>Severity<select value={severity} onChange={(event) => setSeverity(event.target.value)}>{severities.map((item) => <option key={item}>{item}</option>)}</select></label>
      <label>Review state<select value={state} onChange={(event) => setState(event.target.value)}>{["ALL", "UNREAD", "REVIEWED", "PINNED", "DISMISSED", "NOT_RELEVANT", "APPLIED"].map((item) => <option key={item}>{item}</option>)}</select></label>
      <label>Claim<select value={claim} onChange={(event) => setClaim(event.target.value)}><option>ALL</option>{[...new Set(alerts.map((item) => item.claim_id))].map((id) => <option key={id}>{id}</option>)}</select></label></div>
    {visible.length ? <div className="matter-cards">{visible.map((item) => <article key={item.alert_id}>
      <p className="eyebrow">{item.severity} · {item.review_state}</p><h3>{item.title}</h3><p>{item.explanation}</p><p>Case {item.new_case_id} · claim {item.claim_id}</p>
      <button onClick={() => void api<AlertDetail>(`/matters/${matterId}/alerts/${item.alert_id}`).then(setSelected).catch(() => setError("Impact could not be loaded."))}>Review impact</button>
    </article>)}</div> : <p>No alerts match. A monitoring run can correctly find no material change.</p>}
    {selected && <article className="claim-detail" aria-label="Change impact">
      <div className="claim-detail-head"><h3>What changed · {selected.alert.severity}</h3><button onClick={() => setSelected(null)}>Close</button></div>
      <p>{selected.alert.title}</p><p>Event: {selected.event?.event_type ?? "Unavailable"} · impact: {selected.impact?.category ?? "Unassessed"}</p>
      <p>Prior claim status: {selected.impact?.previous_finding_status ?? "Unknown"} · prior coverage: {selected.impact?.previous_coverage ?? "Unknown"}</p>
      <p>Doctrine before: {selected.impact?.previous_doctrine ?? "Not reviewed"} · after: {selected.impact?.new_doctrine ?? "Not reassessed"}</p>
      <p>Authority relationship: {selected.impact?.authority_relationship ?? "Unknown"} · treatment: {selected.impact?.treatment_relationship ?? "Not confirmed"}</p>
      {selected.event?.treatment && <p>Observed citation language: {selected.event.treatment.generated_label} · {selected.event.treatment.review_state}. Review the exact context before relying on this treatment.</p>}
      <p>{selected.impact?.explanation}</p>
      {selected.event?.result && <><h4>Exact new authority passage</h4><p>{selected.event.result.case_name} · {selected.event.result.court}</p>
        <blockquote>{selected.event.result.relevant_passage.text}</blockquote><p>Passage {selected.event.result.relevant_passage.passage_id} · characters {selected.event.result.relevant_passage.start}–{selected.event.result.relevant_passage.end}</p>
        <a href={selected.event.result.source_url} target="_blank" rel="noreferrer">Open source ↗</a></>}
      <div className="review-actions"><button onClick={() => openClaim(selected.alert.claim_id)}>Open affected claim</button>
        <button onClick={() => void review(selected.alert.alert_id, "REVIEWED")}>Mark reviewed</button>
        <button onClick={() => void review(selected.alert.alert_id, "PINNED")}>Pin</button>
        <button onClick={() => void review(selected.alert.alert_id, "DISMISSED")}>Dismiss</button>
        <button onClick={() => void review(selected.alert.alert_id, "NOT_RELEVANT")}>Not relevant</button>
        <button onClick={() => void apply(selected.alert.alert_id)}>Apply to claim</button>
        <button onClick={() => void api(`/matters/${matterId}/alerts/${selected.alert.alert_id}/deep-research`, { method: "POST" }).catch(() => setError("Approve a claim research plan before running it."))}>Run Deep Research</button></div>
    </article>}
  </section>;
}
