"use client";

import Link from "next/link";
import { ChangeEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { Claim, Document, DocumentDetail, Evidence, Finding, Issue, Matter, uploadDocument } from "@/lib/matter";

type Job = { job_id: string; status: string; error_code: string | null };
type Tab = "documents" | "issues" | "xray" | "authorities";

export function MatterWorkspace({ matterId }: { matterId: string }) {
  const [matter, setMatter] = useState<Matter | null>(null);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [claims, setClaims] = useState<Claim[]>([]);
  const [issues, setIssues] = useState<Issue[]>([]);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<DocumentDetail | null>(null);
  const [tab, setTab] = useState<Tab>("documents");
  const [filter, setFilter] = useState("ALL");
  const [issueFilter, setIssueFilter] = useState("ALL");
  const [sort, setSort] = useState("source");
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [proposition, setProposition] = useState("");

  const refresh = useCallback(async () => {
    const [nextMatter, nextDocs, nextClaims, nextFindings, nextIssues] = await Promise.all([
      api<Matter>(`/matters/${matterId}`),
      api<Document[]>(`/matters/${matterId}/documents`),
      api<Claim[]>(`/matters/${matterId}/claims`),
      api<Finding[]>(`/matters/${matterId}/argument-xray`),
      api<Issue[]>(`/matters/${matterId}/issues`),
    ]);
    setMatter(nextMatter); setDocuments(nextDocs); setClaims(nextClaims); setFindings(nextFindings); setIssues(nextIssues);
  }, [matterId]);

  useEffect(() => {
    void Promise.resolve().then(refresh).catch(() => setError("Matter could not be loaded."));
  }, [refresh]);
  useEffect(() => {
    if (!job || job.status === "completed" || job.status === "failed") return;
    const timer = window.setInterval(() => {
      void api<Job>(`/matters/${matterId}/jobs/${job.job_id}`).then((next) => {
        setJob(next);
        if (next.status === "completed") void refresh();
      }).catch(() => setError("Analysis status could not be loaded."));
    }, 1500);
    return () => window.clearInterval(timer);
  }, [job, matterId, refresh]);

  const selectedClaim = claims.find((claim) => claim.claim_id === selectedId);
  const selectedFinding = findings.find((finding) => finding.claim_id === selectedId);
  const selectedDocumentId = selectedClaim?.document_id;
  useEffect(() => {
    if (!selectedDocumentId) return;
    void api<DocumentDetail>(`/matters/${matterId}/documents/${selectedDocumentId}`)
      .then(setDetail).catch(() => setError("Document source could not be loaded."));
  }, [matterId, selectedDocumentId]);

  function openClaim(claimId: string) {
    const claim = claims.find((item) => item.claim_id === claimId);
    setSelectedId(claimId);
    setProposition(claim?.normalized_proposition ?? "");
  }

  const visible = useMemo(() => {
    const rows = findings.filter((finding) =>
      (filter === "ALL" || finding.vulnerability === filter) &&
      (issueFilter === "ALL" || finding.issue_id === issueFilter));
    return rows.sort((a, b) => sort === "status"
      ? a.vulnerability.localeCompare(b.vulnerability)
      : a.source.start - b.source.start);
  }, [findings, filter, issueFilter, sort]);
  const issueIds = issues.map((issue) => issue.issue_id);

  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    setBusy(true); setError("");
    try { await uploadDocument(matterId, file); await refresh(); }
    catch { setError("Upload failed. Use a valid PDF, DOCX, TXT, or Markdown file under 10 MB."); }
    finally { setBusy(false); event.target.value = ""; }
  }

  async function analyze(documentId: string) {
    setError("");
    try {
      const accepted = await api<Job>(`/matters/${matterId}/documents/${documentId}/analyze`, { method: "POST" });
      setJob(accepted);
    } catch { setError("Analysis could not start. Check the backend LLM configuration."); }
  }

  async function saveClaim() {
    if (!selectedClaim) return;
    try {
      await api(`/matters/${matterId}/claims/${selectedClaim.claim_id}`, {
        method: "PATCH", body: JSON.stringify({ normalized_proposition: proposition }),
      });
      await refresh();
    } catch { setError("Claim edit could not be saved."); }
  }

  async function markIrrelevant() {
    if (!selectedClaim) return;
    try {
      await api(`/matters/${matterId}/claims/${selectedClaim.claim_id}`, {
        method: "PATCH", body: JSON.stringify({ irrelevant: !selectedClaim.irrelevant }),
      });
      await refresh();
    } catch { setError("Claim review could not be saved."); }
  }

  async function reanalyze() {
    if (!selectedClaim) return;
    try {
      setJob(await api<Job>(`/matters/${matterId}/claims/${selectedClaim.claim_id}/reanalyze`, { method: "POST" }));
    } catch { setError("Claim analysis could not start."); }
  }

  async function editAuthority(caseId: string, evidenceId: string | null, update: { pinned?: boolean; removed?: boolean }) {
    if (!selectedClaim) return;
    try {
      await api(`/matters/${matterId}/claims/${selectedClaim.claim_id}/authorities/${caseId}`, {
        method: "PATCH", body: JSON.stringify({ ...update, evidence_id: evidenceId }),
      });
      await refresh();
    } catch { setError("Authority review could not be saved."); }
  }

  return <>
    <div className="matter-heading"><div><p className="eyebrow">Matter workspace</p><h1>{matter?.name ?? "Loading matter…"}</h1><p>{matter?.court || matter?.jurisdiction || "Forum unspecified"}</p></div><Link href="/matters">All matters →</Link></div>
    <div className="matter-tabs" role="tablist">{(["documents", "issues", "xray", "authorities"] as Tab[]).map((name) =>
      <button key={name} type="button" role="tab" aria-selected={tab === name} onClick={() => setTab(name)}>{name === "xray" ? "Argument X-Ray" : name}</button>)}</div>
    {error && <p role="alert" className="notice">{error}</p>}
    {job && <p role="status" className="notice">Analysis: {job.status}{job.error_code ? ` (${job.error_code})` : ""}</p>}

    {tab === "documents" && <section className="workspace"><h2>Documents</h2><p>Uploaded files stay in private local runtime storage. Scanned PDFs require OCR and are not analyzed.</p>
      <label className="upload-control">Upload PDF, DOCX, TXT, or Markdown<input type="file" accept=".pdf,.docx,.txt,.md" disabled={busy} onChange={upload} /></label>
      <div className="matter-cards">{documents.map((doc) => <article key={doc.document_id}><h3>{doc.filename}</h3><p>{doc.document_type.toUpperCase()} · {doc.page_count ? `${doc.page_count} pages` : "Pages unavailable"} · {doc.ingestion_status}</p>{doc.analysis_warnings?.map((warning) => <p key={warning} className="notice">{warning}</p>)}<button disabled={doc.ingestion_status !== "READY" || job?.status === "running"} onClick={() => void analyze(doc.document_id)}>Analyze document</button></article>)}</div>
    </section>}

    {tab === "issues" && <section className="workspace"><h2>Legal issues</h2><p>Each issue groups claims extracted from uploaded writing. Review source text before relying on any finding.</p><div className="matter-cards">{issues.map((issue) => <article key={issue.issue_id}><h3>{issue.label}</h3><p>{claims.filter((claim) => claim.issue_id === issue.issue_id).length} claims</p>{issue.uncertainty && <p>Uncertainty: {issue.uncertainty}</p>}</article>)}</div></section>}

    {tab === "xray" && <section className="workspace"><h2>Argument X-Ray</h2><p>Categories describe evidence coverage in this local corpus; they do not predict a legal outcome.</p>
      <div className="matrix-controls"><label>Status<select value={filter} onChange={(event) => setFilter(event.target.value)}><option>ALL</option>{["STRONG", "MIXED", "VULNERABLE", "UNSUPPORTED", "INSUFFICIENT_EVIDENCE"].map((value) => <option key={value}>{value}</option>)}</select></label><label>Issue<select value={issueFilter} onChange={(event) => setIssueFilter(event.target.value)}><option>ALL</option>{issueIds.map((id) => <option key={id} value={id}>{issues.find((issue) => issue.issue_id === id)?.label ?? id}</option>)}</select></label><label>Sort<select value={sort} onChange={(event) => setSort(event.target.value)}><option value="source">Document order</option><option value="status">Status</option></select></label></div>
      <div className="matrix-scroll"><table className="evidence-matrix"><thead><tr><th>Issue</th><th>Claim</th><th>Document source</th><th>Cited authority</th><th>Citation verification</th><th>Strongest support</th><th>Counter-authority</th><th>Status</th></tr></thead><tbody>{visible.map((finding) => <tr key={finding.claim_id}><td>{issues.find((issue) => issue.issue_id === finding.issue_id)?.label ?? "Unassigned"}</td><td><button className="link-button" onClick={() => openClaim(finding.claim_id)}>{finding.proposition}</button></td><td>{documents.find((doc) => doc.document_id === finding.source.document_id)?.filename ?? "Document"}{finding.source.page ? ` · p. ${finding.source.page}` : ""}</td><td>{finding.evidence.find((e) => e.role === "cited")?.result.case_name ?? "Unresolved"}</td><td>{finding.cited_authorities.filter((link) => link.citation_id).map((link) => link.relation).join(", ") || "Unjudged"}</td><td>{finding.evidence.find((e) => e.role === "independent_support")?.result.case_name ?? "None"}</td><td>{finding.counter_authorities[0]?.evidence.result.case_name ?? "None verified"}</td><td><span className={`finding-status ${finding.vulnerability.toLowerCase()}`}>{finding.vulnerability}</span></td></tr>)}</tbody></table></div>
      {visible.length === 0 && <p>No findings match these filters. Analyze a document to populate the X-Ray.</p>}
    </section>}

    {tab === "authorities" && <section className="workspace"><h2>Authorities</h2><p>These cases are drawn from the local LexTrace corpus. Cited, independently found, and counter-authority roles stay separate.</p><div className="authorities-list">{findings.flatMap((finding) => finding.evidence.map((evidence) => <EvidenceCard key={`${finding.claim_id}-${evidence.evidence_id}`} evidence={evidence} />))}</div></section>}

    {selectedClaim && <section className="claim-detail" aria-label="Claim detail"><div className="claim-detail-head"><div><p className="eyebrow">Claim detail · provenance</p><h2>{selectedClaim.normalized_proposition}</h2></div><button className="close-button" onClick={() => setSelectedId(null)}>Close</button></div>
      <h3>Exact document source</h3><p>{documents.find((doc) => doc.document_id === selectedClaim.document_id)?.filename} {selectedClaim.span.page ? `· page ${selectedClaim.span.page}` : ""} · characters {selectedClaim.span.start}–{selectedClaim.span.end}</p>
      {detail?.document.document_id === selectedClaim.document_id && <div className="source-viewer">{detail.text.slice(0, selectedClaim.span.start)}<mark>{detail.text.slice(selectedClaim.span.start, selectedClaim.span.end)}</mark>{detail.text.slice(selectedClaim.span.end)}</div>}
      <h3>Normalized proposition</h3><textarea value={proposition} onChange={(event) => setProposition(event.target.value)} /><div className="review-actions"><button onClick={() => void saveClaim()}>Save edit</button><button onClick={() => void markIrrelevant()}>{selectedClaim.irrelevant ? "Restore claim" : "Mark irrelevant"}</button><button onClick={() => void reanalyze()}>Re-analyze this claim</button></div>
      {selectedFinding ? <><h3>Why this finding?</h3><p><span className={`finding-status ${selectedFinding.vulnerability.toLowerCase()}`}>{selectedFinding.vulnerability}</span> {selectedFinding.explanation}</p><p>Verification: {selectedFinding.verification_status} · Research: {selectedFinding.research_coverage}</p>{selectedFinding.unresolved_citation_ids.length > 0 && <p className="notice">{selectedFinding.unresolved_citation_ids.length} unresolved citation reference(s). Additional research is needed.</p>}
        <h3>Exact case-law passages</h3>{selectedFinding.evidence.map((evidence) => <div key={evidence.evidence_id}>{!selectedFinding.cited_authorities.some((link) => link.evidence_id === evidence.evidence_id && link.removed) && <EvidenceCard evidence={evidence} />}{selectedFinding.cited_authorities.filter((link) => link.evidence_id === evidence.evidence_id).map((link) => <div key={`${link.case_id}-${link.relation}-${link.evidence_id}`} className="review-actions"><span>{link.removed ? "Removed by reviewer" : link.relation}</span><button onClick={() => void editAuthority(link.case_id, link.evidence_id, { pinned: !link.pinned })}>{link.pinned ? "Unpin" : "Pin"}</button><button onClick={() => void editAuthority(link.case_id, link.evidence_id, { removed: !link.removed })}>{link.removed ? "Restore" : "Remove suggestion"}</button></div>)}</div>)}</> : <p>This claim needs re-analysis after the latest edit.</p>}
    </section>}
  </>;
}

function EvidenceCard({ evidence }: { evidence: Evidence }) {
  const item = evidence.result;
  return <article className="evidence-card"><p className="eyebrow">{evidence.role.replaceAll("_", " ")} · {item.court} · {item.date_filed ?? "date unknown"}</p><h3>{item.case_name}</h3><p>{item.reporter_citations?.join(", ")}</p><blockquote>{item.relevant_passage.text}</blockquote><p className="evidence-meta">Case {item.case_id} · Opinion {item.relevant_passage.opinion_id} · Passage {item.relevant_passage.passage_id}</p><a href={item.source_url} target="_blank" rel="noreferrer">Open CourtListener source ↗</a></article>;
}
