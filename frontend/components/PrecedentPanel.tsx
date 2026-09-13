"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ClaimAuthorityAnalysis, DoctrineAnalysis, TraceEdge, TraceNode } from "@/lib/precedent";

export function PrecedentPanel({ matterId, claimId }: { matterId: string; claimId: string }) {
  const [analysis, setAnalysis] = useState<DoctrineAnalysis | null>(null);
  const [authorities, setAuthorities] = useState<ClaimAuthorityAnalysis | null>(null);
  const [selected, setSelected] = useState<TraceEdge | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const base = `/matters/${matterId}/claims/${claimId}`;

  const refresh = useCallback(async () => {
    setBusy(true); setError("");
    try {
      const [nextDoctrine, nextAuthorities] = await Promise.all([
        api<DoctrineAnalysis>(`${base}/doctrine`),
        api<ClaimAuthorityAnalysis>(`${base}/authority-analysis`),
      ]);
      if (!nextDoctrine?.trace?.nodes || !nextAuthorities?.relationships) {
        throw new Error("Invalid precedent response.");
      }
      setAnalysis(nextDoctrine); setAuthorities(nextAuthorities);
      setSelected((previous) => previous
        ? nextDoctrine.trace.edges.find((item) => item.treatment.annotation_id === previous.treatment.annotation_id) ?? null
        : null);
    } catch { setError("Precedent history is unavailable for this claim or local graph."); }
    finally { setBusy(false); }
  }, [base]);

  useEffect(() => { void Promise.resolve().then(refresh); }, [refresh]);

  async function review(edge: TraceEdge, state: "CONFIRMED" | "REJECTED" | "UNCERTAIN") {
    setError("");
    try {
      await api(`${base}/treatments/${edge.treatment.annotation_id}`, {
        method: "PATCH", body: JSON.stringify({ state }),
      });
      await refresh();
    } catch { setError("Treatment review could not be saved."); }
  }

  const nodes = new Map(analysis?.trace.nodes.map((item) => [item.node.case_id, item]) ?? []);
  const trace = analysis?.trace;
  return <section className="precedent-panel" aria-label="Precedent intelligence">
    <h3>Authority intelligence</h3>
    <p>Relevance, court hierarchy, and later treatment are separate signals. None proves how a court will decide this claim.</p>
    {busy && <p role="status">Tracing local precedent…</p>}
    {error && <p role="alert">{error}</p>}
    {authorities?.relationships.map((item) =>
      <article className="precedent-card" key={item.case_id}><strong>Case {item.case_id} · {item.category}</strong><p>{item.rationale}</p><small>{item.uncertainty.join(" ")}</small></article>)}
    {analysis && <>
      <h3>Precedent Trace</h3>
      <p>Earlier and later local citation links around case {trace?.seed_case_id}. Select a relationship to inspect evidence.</p>
      <div className="trace-columns">
        <div><h4>Earlier authority</h4>{trace?.edges.filter((item) => item.edge.citing_case_id === trace.seed_case_id).map((item) => <TraceRelation key={item.treatment.annotation_id} edge={item} node={nodes.get(item.edge.cited_case_id)} onSelect={setSelected} />)}</div>
        <div><h4>Seed authority</h4><TraceCase node={nodes.get(trace?.seed_case_id ?? "")} /></div>
        <div><h4>Later citing authority</h4>{trace?.edges.filter((item) => item.edge.cited_case_id === trace.seed_case_id).map((item) => <TraceRelation key={item.treatment.annotation_id} edge={item} node={nodes.get(item.edge.citing_case_id)} onSelect={setSelected} />)}</div>
      </div>
      <p className="evidence-meta">{trace?.nodes_examined} nodes · {trace?.edges_examined} edges examined · {trace?.context_recovered} contexts recovered</p>
      {trace?.warnings.map((warning) => <p className="notice" key={warning}>{warning}</p>)}
      {selected && <article className="evidence-card" aria-label="Relationship evidence"><h4>{selected.treatment.generated_label} · {selected.treatment.review_state}</h4><p>Case {selected.edge.citing_case_id} cites case {selected.edge.cited_case_id}. {selected.treatment.review_requirement.replaceAll("_", " ")}.</p>
        {selected.context ? <><blockquote>{selected.context.passage.text}</blockquote><p className="evidence-meta">Opinion {selected.context.citing_opinion_id} · Passage {selected.context.passage.passage_id} · characters {selected.context.passage.start}–{selected.context.passage.end}</p><a href={selected.context.source_url} target="_blank" rel="noreferrer">Open citing opinion ↗</a></> : <p>Citation context unavailable; treatment remains unverified.</p>}
        <div className="review-actions"><button onClick={() => void review(selected, "CONFIRMED")}>Confirm</button><button onClick={() => void review(selected, "REJECTED")}>Reject</button><button onClick={() => void review(selected, "UNCERTAIN")}>Mark uncertain</button></div>
      </article>}
      <h3>Relevant retrieved doctrinal history</h3>
      {analysis.events.length ? <ol className="doctrine-timeline">{analysis.events.map((event) => <li key={event.event_id}><button onClick={() => setSelected(trace?.edges.find((item) => item.treatment.annotation_id === event.annotation_id) ?? null)}><strong>{event.date_filed.slice(0, 4)} · {event.case_name ?? event.case_id}</strong><span>{event.category.replaceAll("_", " ")} · {event.court ?? "court unknown"}</span></button><p>{event.uncertainty.join(" ")}</p></li>)}</ol> : <p>No evidence-backed doctrine event was recovered from this local graph.</p>}
      <p className="notice">{analysis.state.coverage_warning}</p>
      <h3>Impact on argument</h3><p><strong>{analysis.impact.category}</strong> · {analysis.impact.explanation}</p>
      {analysis.impact.research_gaps.map((gap) => <p key={gap}>{gap}</p>)}
    </>}
  </section>;
}

function TraceCase({ node }: { node: TraceNode | undefined }) {
  if (!node) return <p>Case unavailable in local graph.</p>;
  return <article className="precedent-card"><strong>{node.node.case_name ?? `Case ${node.node.case_id}`}</strong><p>{node.node.court ?? "Court unknown"} · {node.node.date_filed?.slice(0, 4) ?? "Date unknown"}</p><p>{node.authority.category} · {node.relevant_passage ? "Proposition match found" : "Proposition match unavailable"}</p></article>;
}

function TraceRelation({ edge, node, onSelect }: { edge: TraceEdge; node: TraceNode | undefined; onSelect: (edge: TraceEdge) => void }) {
  return <button className="precedent-card trace-relation" onClick={() => onSelect(edge)}><strong>{node?.node.case_name ?? `Case ${node?.node.case_id ?? "?"}`}</strong><span>{node?.node.court ?? "Court unknown"} · {node?.node.date_filed?.slice(0, 4) ?? "Date unknown"}</span><span>{node?.authority.category ?? "UNKNOWN"} · {edge.treatment.generated_label} · {edge.treatment.review_state}</span></button>;
}
