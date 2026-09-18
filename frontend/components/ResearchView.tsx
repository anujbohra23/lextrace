"use client";

import { useRouter } from "next/navigation";
import { FormEvent, useRef, useState } from "react";
import { api, safeError } from "@/lib/api";
import { CourtSelect } from "./CourtSelect";
import { CorpusCoverage } from "./CorpusCoverage";
import { RunHistory } from "./ResearchRun";

export function ResearchView() {
  const router = useRouter();
  const [question, setQuestion] = useState("");
  const [jurisdiction, setJurisdiction] = useState("");
  const [asOf, setAsOf] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submitting = useRef(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true; setBusy(true); setError("");
    try {
      const accepted = await api<{run_id: string}>("/research", {method: "POST", body: JSON.stringify({question, jurisdiction: jurisdiction || null, as_of_date: asOf || null})});
      router.push(`/runs/${accepted.run_id}`);
    } catch (error) { setError(safeError(error)); submitting.current = false; setBusy(false); }
  }
  return <>
    <section className="hero compact"><p className="eyebrow">Research</p><h1>Research a question</h1><p>Describe the issue and facts. Review the answer against its sources before relying on it.</p></section>
    <CorpusCoverage />
    <form onSubmit={submit}>
      <label>Legal question or fact pattern<textarea required maxLength={4000} value={question} onChange={event => setQuestion(event.target.value)} placeholder="What limits apply to restrictions on truthful attorney advertising?" /></label>
      <div className="filters"><CourtSelect value={jurisdiction} onChange={setJurisdiction} /><label>As of (optional)<input type="date" value={asOf} onChange={event => setAsOf(event.target.value)} /></label></div>
      <p>Research runs in the background. You can leave the run page and return from recent research.</p>
      <button disabled={busy}>{busy ? "Researching…" : "Start research"}</button>
      {error && <p role="alert">{error}</p>}
    </form>
    <RunHistory />
  </>;
}

export { Result } from "./ResearchResult";
