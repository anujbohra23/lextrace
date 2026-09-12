"use client";

import { FormEvent, useState } from "react";
import { api, Evidence } from "@/lib/api";

export default function Search() {
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState("reranked");
  const [results, setResults] = useState<Evidence["result"][]>([]);
  const [failed, setFailed] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setFailed(false);
    try {
      const data = await api<{ results: Evidence["result"][] }>("/search", {
        method: "POST", body: JSON.stringify({ query, mode, top_k: 10 }),
      });
      setResults(data.results);
    } catch { setFailed(true); }
  }
  return <>
    <section className="hero compact"><p className="eyebrow">Retrieval explorer</p><h1>Search the precedent corpus.</h1></section>
    <form className="search-form" onSubmit={submit}>
      <input aria-label="Search query" required value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Fourth Amendment search or seizure" />
      <select aria-label="Retrieval mode" value={mode} onChange={(event) => setMode(event.target.value)}><option value="bm25">BM25</option><option value="dense">Dense</option><option value="hybrid">Hybrid</option><option value="reranked">Reranked</option><option value="citation_reranked">Citation reranked</option></select>
      <button>Search</button>
    </form>
    {failed && <p role="alert">Search failed.</p>}
    <div className="authorities">{results.map((item) => <article key={item.case_id}><p className="eyebrow">Ranked score {item.final_score.toFixed(4)}</p><h3>{item.case_name}</h3><p>{item.court} · {item.reporter_citations?.join(", ")}</p><blockquote>{item.relevant_passage.text}</blockquote><a href={item.source_url} target="_blank" rel="noreferrer">View source ↗</a></article>)}</div>
  </>;
}
