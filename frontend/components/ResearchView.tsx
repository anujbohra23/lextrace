"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";
import { api, ResearchResult } from "@/lib/api";

type RunEnvelope = {
  run_id: string;
  status: string;
  result: ResearchResult | null;
};

export function ResearchView() {
  const [question, setQuestion] = useState("");
  const [jurisdiction, setJurisdiction] = useState("");
  const [asOf, setAsOf] = useState("");
  const [status, setStatus] = useState<"idle" | "loading" | "failed">("idle");
  const [result, setResult] = useState<ResearchResult | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setStatus("loading");
    setResult(null);
    try {
      const accepted = await api<{ run_id: string }>("/research", {
        method: "POST",
        body: JSON.stringify({
          question,
          jurisdiction: jurisdiction || null,
          as_of_date: asOf || null,
        }),
      });
      for (let attempt = 0; attempt < 120; attempt += 1) {
        const run = await api<RunEnvelope>(`/research/${accepted.run_id}`);
        if (run.result) {
          setResult(run.result);
          setStatus("idle");
          return;
        }
        if (run.status === "failed") throw new Error("failed");
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
      throw new Error("timeout");
    } catch {
      setStatus("failed");
    }
  }

  return <>
    <section className="hero">
      <p className="eyebrow">Evidence-grounded research workspace</p>
      <h1>Trace legal conclusions back to precedent.</h1>
      <p>Research U.S. case law with visible evidence, competing arguments, and claim-level verification.</p>
    </section>
    <form className="research-form" onSubmit={submit}>
      <label>Legal question or fact pattern
        <textarea required maxLength={4000} value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="An employee was fired after discussing salary with coworkers…" />
      </label>
      <div className="filters">
        <label>Jurisdiction<input value={jurisdiction} onChange={(event) => setJurisdiction(event.target.value)} placeholder="ca2" /></label>
        <label>As of<input type="date" value={asOf} onChange={(event) => setAsOf(event.target.value)} /></label>
      </div>
      <button disabled={status === "loading"}>{status === "loading" ? "Researching…" : "Research"}</button>
      {status === "failed" && <p role="alert">Research could not be completed. Check the runtime configuration and try again.</p>}
    </form>
    {result && <Result result={result} />}
  </>;
}

export function Result({ result }: { result: ResearchResult }) {
  const memo = result.final_memo;
  return <section className="workspace">
    <div className="summary"><span>{result.grounding_summary.confidence}</span><strong>{result.grounding_summary.supported}/{result.grounding_summary.total_substantive_claims}</strong><small>supported claims</small></div>
    <h2>Issues</h2>
    <div className="cards">{result.identified_issues.map((issue) => <article key={issue.issue_id}><h3>{issue.label}</h3><p>{issue.description}</p></article>)}</div>
    <h2>Relevant precedent</h2>
    <div className="authorities">{result.relevant_cases.map(({ result: item }) => <article key={item.case_id}><div><p className="eyebrow">{item.court} · {item.date_filed ?? "date unavailable"}</p><h3>{item.case_name}</h3><p>{item.reporter_citations?.join(", ")}</p></div><blockquote>{item.relevant_passage.text}</blockquote><a href={item.source_url} target="_blank" rel="noreferrer">View source ↗</a></article>)}</div>
    {memo && <><h2>Analysis</h2>{memo.analysis.map((text) => <p key={text}>{text}</p>)}<div className="columns"><section><h2>Supporting argument</h2>{memo.supporting_arguments.map((text) => <p key={text}>{text}</p>)}</section><section><h2>Counterargument</h2>{memo.counterarguments.map((text) => <p key={text}>{text}</p>)}</section></div><h2>Conclusion</h2><p>{memo.research_conclusion}</p><p className="disclaimer">{memo.disclaimer}</p></>}
    <h2>Verification</h2>
    {result.verification_results.map((item) => <p key={item.claim_id}><span className="status">{item.status}</span> {item.explanation}</p>)}
    <Link href={`/runs/${result.run_id}`}>View safe execution trace →</Link>
  </section>;
}
