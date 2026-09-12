"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type Trace = {
  status: string;
  provider?: string;
  model?: string;
  nodes_executed?: string[];
  node_seconds?: Record<string, number>;
  evidence_count?: number;
  claims_generated?: number;
  claims_supported?: number;
  cache_hits?: number;
  cache_misses?: number;
  usage?: { calls: number; input_tokens: number; output_tokens: number };
};

export default function RunDetails({ params }: { params: Promise<{ id: string }> }) {
  const [trace, setTrace] = useState<Trace | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    params.then(({ id }) => api<Trace>(`/research/${id}/trace`)).then(setTrace).catch(() => setFailed(true));
  }, [params]);
  if (failed) return <p role="alert">Run trace unavailable.</p>;
  if (!trace) return <p>Loading run trace…</p>;
  return <>
    <section className="hero compact"><p className="eyebrow">Safe execution trace</p><h1>{trace.status}</h1><p>{trace.provider} · {trace.model}</p></section>
    <div className="trace-grid"><article><strong>{trace.evidence_count ?? 0}</strong><span>Evidence items</span></article><article><strong>{trace.claims_supported ?? 0}/{trace.claims_generated ?? 0}</strong><span>Supported claims</span></article><article><strong>{trace.usage?.calls ?? 0}</strong><span>LLM calls</span></article><article><strong>{trace.cache_hits ?? 0}/{trace.cache_misses ?? 0}</strong><span>Cache hits / misses</span></article></div>
    <h2>Workflow nodes</h2>
    <ol className="timeline">{trace.nodes_executed?.map((node) => <li key={node}><span>{node.replaceAll("_", " ")}</span><small>{(trace.node_seconds?.[node] ?? 0).toFixed(3)}s</small></li>)}</ol>
  </>;
}
