"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, ResearchResult, safeError } from "@/lib/api";
import { Result } from "./ResearchResult";
import { label } from "@/lib/labels";
export type Run = { run_id: string; status: string; created_at: string; error_code?: string | null; can_resume?: boolean; progress?: {stage?: string; updated_at?: string} | null; result?: ResearchResult | null };
const active = (status: string) => ["queued", "running", "stopping"].includes(status);
export function RunHistory() {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { let mounted = true; api<Run[]>("/runs").then(value => { if (mounted) { if (Array.isArray(value)) setRuns(value); else setError("Recent research could not be loaded."); } }).catch(() => { if (mounted) setError("Recent research is unavailable. Retry by refreshing this page."); }); return () => { mounted = false; }; }, []);
  return <section className="workspace"><h2>Recent research</h2>{error ? <p role="alert">{error}</p> : !runs ? <p>Loading saved runs…</p> : !runs.length ? <p>No research yet. Start with a question above.</p> : <ul className="run-list">{runs.map(run => <li key={run.run_id}><Link href={`/runs/${run.run_id}`}>Research · {new Date(run.created_at).toLocaleString()}</Link> <span>{label(run.status)}</span></li>)}</ul>}</section>;
}
export function ResearchRun({runId}: {runId: string}) {
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(0);
  useEffect(() => {
    let mounted = true; let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const value = await api<Run>(`/research/${runId}`);
        if (!mounted) return;
        setRun(value); setError(""); setNow(Date.now());
        if (active(value.status)) timer = setTimeout(poll, 2500);
      } catch (error) { if (mounted) { setError(safeError(error)); timer = setTimeout(poll, 5000); } }
    }
    void poll(); return () => { mounted = false; clearTimeout(timer); };
  }, [runId, revision]);
  async function resume() {
    setBusy(true);
    try { await api(`/research/${runId}/resume`, {method: "POST"}); setRevision(value => value + 1); }
    catch (error) { setError(safeError(error)); }
    finally { setBusy(false); }
  }
  async function stop() {
    setBusy(true);
    try { await api(`/research/${runId}/stop`, {method: "POST"}); setRevision(value => value + 1); }
    catch (error) { setError(safeError(error)); }
    finally { setBusy(false); }
  }
  return <>
    <Link href="/research">← Research and saved runs</Link>
    <section className="hero compact"><h1>Research progress</h1><p>This page is saved. You can refresh or return later without starting another run.</p></section>
    {error && <p role="alert">{error} <button onClick={() => setRevision(value => value + 1)}>Check again</button></p>}
    {!run || run.run_id !== runId ? <p role="status">Loading research…</p> : <>
      <section className="coverage-note" role="status"><strong>{run.error_code === "cancelled" ? "Stopped" : label(run.status)}</strong>
        {active(run.status) && <><p>{run.status === "stopping" ? "Stopping after the current model call or stage. This may take a few minutes." : run.status === "queued" ? "Waiting for an available research worker. You do not need to resubmit." : run.progress?.stage ? label(run.progress.stage) : "Starting research…"}</p><p>{Math.max(0, Math.floor((now - Date.parse(run.created_at)) / 60000))} minutes since submission. Local models can take several minutes.</p>{run.progress?.updated_at && <p>Last progress: {new Date(run.progress.updated_at).toLocaleTimeString()}</p>}</>}
        {active(run.status) && run.status !== "stopping" && <button disabled={busy} onClick={() => void stop()}>Stop research</button>}
        {run.status === "failed" && <p>{run.error_code === "cancelled" ? "Research was stopped at your request." : run.error_code === "interrupted" ? "The backend restarted before this run finished." : "The workflow could not finish. Check the local provider and retry this saved run."}</p>}
        {run.status === "degraded" && <p>Partial result: some steps could not be completed. Review the limitations and evidence.</p>}
        {["failed", "degraded"].includes(run.status) && run.can_resume && <button disabled={busy} onClick={() => void resume()}>Retry saved research</button>}
        {!active(run.status) && !run.result && run.status !== "failed" && <p>Run finished, but result content was not retained. Check content-persistence settings.</p>}
      </section>
      {run.result && <Result result={run.result} />}
    </>}
  </>;
}
