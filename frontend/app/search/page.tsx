"use client";

import { FormEvent, useState } from "react";
import { CorpusCoverage } from "@/components/CorpusCoverage";
import { api, Evidence, safeError } from "@/lib/api";

export default function Search() {
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState("reranked");
  const [results, setResults] = useState<Evidence["result"][]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [searched, setSearched] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError(""); setBusy(true); setResults([]);
    try {
      const data = await api<{ results: Evidence["result"][] }>("/search", {
        method: "POST", body: JSON.stringify({ query, mode, top_k: 10 }),
      });
      setResults(data.results);
      setSearched(true);
    } catch (error) { setError(safeError(error)); } finally { setBusy(false); }
  }
  return <>
    <section className="hero compact"><p className="eyebrow">Find sources</p><h1>Find cases in your collection.</h1></section>
    <CorpusCoverage />
    <form className="search-form" onSubmit={submit}>
      <input aria-label="Search query" required value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Fourth Amendment search or seizure" />
      <details><summary>Advanced search method</summary><select aria-label="Retrieval mode" value={mode} onChange={(event) => setMode(event.target.value)}><option value="bm25">BM25</option><option value="dense">Dense</option><option value="hybrid">Hybrid</option><option value="reranked">Reranked</option><option value="citation_reranked">Citation reranked</option></select></details>
      <button disabled={busy}>{busy ? "Searching…" : "Search"}</button>
    </form>
    {error && <p role="alert">{error}</p>}{searched && !busy && !error && results.length === 0 && <p>No matching cases in this collection. Try different terms or review source coverage.</p>}
    <div className="authorities">{results.map((item) => <article key={item.case_id}><details><summary>Ranking details</summary>Score {item.final_score.toFixed(4)}</details><h3>{item.case_name}</h3><p>{item.court} · {item.reporter_citations?.join(", ")}</p><blockquote>{item.relevant_passage.text}</blockquote><a href={item.source_url} target="_blank" rel="noreferrer">View source ↗</a></article>)}</div>
  </>;
}
