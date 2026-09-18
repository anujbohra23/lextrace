"use client";
import { ResearchResult } from "@/lib/api";
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
    <details><summary>Advanced execution details</summary><p>{result.trace.provider} · {result.trace.model} · {result.trace.total_seconds.toFixed(1)} seconds · {result.trace.usage.calls} model calls</p></details>
  </section>;
}
