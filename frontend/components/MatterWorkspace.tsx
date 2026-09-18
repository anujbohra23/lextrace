"use client";

import Link from "next/link";
import { ChangeEvent, useCallback, useEffect, useRef, useState } from "react";
import { CorpusCoverage } from "./CorpusCoverage";
import { RunHistory } from "./ResearchRun";
import { label } from "@/lib/labels";
import { api } from "@/lib/api";
import { Claim, Document, DocumentDetail, Evidence, Finding, Issue, Matter, uploadDocument } from "@/lib/matter";
import { PrecedentPanel } from "@/components/PrecedentPanel";
import { DeepResearchPanel } from "@/components/DeepResearchPanel";
import { EvidenceMatrixPanel } from "@/components/EvidenceMatrixPanel";
import { MonitoringPanel } from "@/components/MonitoringPanel";
import { MatrixRow } from "@/lib/deepResearch";
import { MatterAlert, MonitoringOverview } from "@/lib/monitoring";

type Job = { job_id: string; status: string; error_code: string | null };
type Tab = "overview" | "documents" | "matrix" | "research";

export function MatterWorkspace({ matterId }: { matterId: string }) {
  const [matter, setMatter] = useState<Matter | null>(null);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [claims, setClaims] = useState<Claim[]>([]);
  const [issues, setIssues] = useState<Issue[]>([]);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [report, setReport] = useState("");
  const [previewId, setPreviewId] = useState<string | null>(null);
  const [detail, setDetail] = useState<DocumentDetail | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const panel = useRef<HTMLElement>(null);
  const [matrixError, setMatrixError] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [reviewNotes, setReviewNotes] = useState("");
  const [proposition, setProposition] = useState("");
  const [matrix, setMatrix] = useState<MatrixRow[]>([]);
  const [monitoring, setMonitoring] = useState<MonitoringOverview | null>(null);
  const [alerts, setAlerts] = useState<MatterAlert[]>([]);

  const refresh = useCallback(async () => {
    setError("");
    const [nextMatter, nextDocs, nextClaims, nextFindings, nextIssues] = await Promise.all([
      api<Matter>(`/matters/${matterId}`),
      api<Document[]>(`/matters/${matterId}/documents`),
      api<Claim[]>(`/matters/${matterId}/claims`),
      api<Finding[]>(`/matters/${matterId}/argument-xray`),
      api<Issue[]>(`/matters/${matterId}/issues`),
    ]);
    setMatter(nextMatter); setDocuments(nextDocs); setClaims(nextClaims); setFindings(nextFindings); setIssues(nextIssues);
    try {
      const rows = await api<MatrixRow[]>(`/matters/${matterId}/evidence-matrix`);
      if (Array.isArray(rows)) { setMatrix(rows); setMatrixError(false); } else { setMatrixError(true); }
    } catch { setMatrixError(true); }
    try {
      const [nextMonitoring, nextAlerts] = await Promise.all([
        api<MonitoringOverview>(`/matters/${matterId}/monitoring`),
        api<MatterAlert[]>(`/matters/${matterId}/alerts`),
      ]);
      setMonitoring(nextMonitoring); setAlerts(nextAlerts);
    } catch { setMonitoring(null); setAlerts([]); }
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

  useEffect(() => {
    try {
      const saved = localStorage.getItem(`lextrace-analysis:${matterId}`);
      if (saved && /^[a-f0-9]{32}$/.test(saved)) void Promise.resolve().then(() => setJob({job_id: saved, status: "queued", error_code: null}));
    } catch { /* Storage may be unavailable; the server still retains the job. */ }
  }, [matterId]);
  useEffect(() => {
    if (!job) return;
    try {
      if (["queued", "running"].includes(job.status)) localStorage.setItem(`lextrace-analysis:${matterId}`, job.job_id);
      else localStorage.removeItem(`lextrace-analysis:${matterId}`);
    } catch { /* Browser storage is optional. */ }
  }, [job, matterId]);

  const selectedClaim = claims.find((claim) => claim.claim_id === selectedId);
  const selectedFinding = findings.find((finding) => finding.claim_id === selectedId);
  const selectedDocumentId = selectedClaim?.document_id ?? previewId;
  useEffect(() => {
    if (!selectedDocumentId) return;
    void api<DocumentDetail>(`/matters/${matterId}/documents/${selectedDocumentId}`)
      .then(setDetail).catch(() => setError("Document source could not be loaded."));
  }, [matterId, selectedDocumentId]);

  useEffect(() => {
    if (!selectedId) return;
    const previous = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") setSelectedId(null); };
    window.addEventListener("keydown", close);
    return () => { window.removeEventListener("keydown", close); previous?.focus(); };
  }, [selectedId]);

  function openClaim(claimId: string) {
    const claim = claims.find((item) => item.claim_id === claimId);
    setSelectedId(claimId);
    setReviewNotes(claim?.review_notes ?? "");
    setProposition(claim?.normalized_proposition ?? "");
  }

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

  async function saveReview(reviewed: boolean) {
    if (!selectedClaim) return;
    try {
      await api(`/matters/${matterId}/claims/${selectedClaim.claim_id}/review`, {method: "PATCH", body: JSON.stringify({reviewed, notes: reviewNotes})});
      await refresh();
    } catch { setError("Your review could not be saved. Please retry."); }
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

  async function toggleLock() {
    if (!selectedClaim) return;
    try {
      await api(`/matters/${matterId}/claims/${selectedClaim.claim_id}`, {
        method: "PATCH", body: JSON.stringify({ wording_locked: !selectedClaim.wording_locked }),
      });
      await refresh();
    } catch { setError("Claim wording lock could not be saved."); }
  }

  async function reanalyze() {
    if (!selectedClaim) return;
    try {
      setJob(await api<Job>(`/matters/${matterId}/claims/${selectedClaim.claim_id}/reanalyze`, { method: "POST" }));
    } catch { setError("Claim analysis could not start."); }
  }

  async function reanalyzeIssue(issueId: string) {
    try {
      setJob(await api<Job>(`/matters/${matterId}/issues/${issueId}/reanalyze`, { method: "POST" }));
    } catch { setError("Issue analysis could not start."); }
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

  async function watch(targetType: "CLAIM" | "AUTHORITY" | "MATTER", referenceId: string) {
    try {
      const existing = monitoring?.targets?.find((target) => target.target_type === targetType && target.target_reference_id === referenceId);
      if (existing) {
        await api(`/matters/${matterId}/monitoring/targets/${existing.target_id}`, {
          method: "PATCH", body: JSON.stringify({ enabled: !existing.enabled }),
        });
      } else {
        await api(`/matters/${matterId}/monitoring/targets`, {
          method: "POST", body: JSON.stringify({ target_type: targetType, target_reference_id: referenceId }),
        });
      }
      await refresh();
    } catch { setError("Watch preference could not be saved."); }
  }

  function watching(targetType: "CLAIM" | "AUTHORITY" | "MATTER", referenceId: string) {
    return monitoring?.targets?.some((target) => target.target_type === targetType && target.target_reference_id === referenceId && target.enabled) ?? false;
  }

  function exportReview() {
    const content = ["# " + (matter?.name ?? "Matter") + " — review report", "Local collection only. Model assessments require human review.", ...findings.map(f => ["## " + f.proposition, "Proposed assessment: " + label(f.vulnerability), f.explanation, "Coverage: " + label(f.research_coverage), "Human review: " + (claims.find(c => c.claim_id === f.claim_id)?.reviewed ? "Reviewed" : "Not reviewed"), "Reviewer notes: " + (claims.find(c => c.claim_id === f.claim_id)?.review_notes || "None"), "Unresolved citations: " + f.unresolved_citation_ids.length, ...f.evidence.map(e => e.result.case_name + "\n" + e.result.source_url + "\n\n" + e.result.relevant_passage.text)].join("\n\n"))].join("\n\n");
    setReport(content);
  }

  return <>
    <div className="matter-heading"><div><p className="eyebrow">Matter workspace</p><h1>{matter?.name ?? "Loading matter…"}</h1><p>{matter?.court || matter?.jurisdiction || "Forum unspecified"}</p><button onClick={() => void watch("MATTER", matterId)}>{watching("MATTER", matterId) ? "Watching Matter · pause" : "Watch Matter"}</button></div><Link href="/matters">All matters →</Link></div>
    <div className="matter-tabs" role="tablist" aria-label="Matter sections">{(["overview", "documents", "matrix", "research"] as Tab[]).map(name =>
      <button key={name} type="button" role="tab" aria-selected={tab === name} onClick={() => setTab(name)}>{name === "matrix" ? "Review" : label(name)}</button>)}</div>
    {error && <p role="alert" className="notice">{error} <button onClick={() => void refresh().catch(() => setError("Matter could not be loaded. Please retry."))}>Retry</button></p>}
    {job && <p role="status" className="notice">Analysis: {job.status}{job.error_code ? ` (${job.error_code})` : ""}</p>}

    {tab === "overview" && matter && <section><h2>Next steps</h2><p>{documents.length} documents · {claims.length} claims · {matrix.reduce((n, row) => n + row.gaps.length, 0)} recorded research gaps</p><div className="task-cards"><button onClick={() => setTab("documents")}>1. Upload or review documents</button><button onClick={() => setTab("matrix")}>2. Review claims and evidence</button><button onClick={() => setTab("research")}>3. Investigate open questions</button></div><CorpusCoverage /><details><summary>Updates and watched authorities</summary><MonitoringPanel matterId={matterId} openClaim={openClaim} onUpdate={() => { void refresh().catch(() => setError("Could not refresh updates.")); }} /></details></section>}
    {tab === "research" && <section><h2>Investigate an open question</h2><p>To investigate a specific claim, select it in Review and choose Investigate this claim.</p><Link href="/research">Research a new question →</Link><details><summary>Issues identified in your documents</summary>{issues.length ? issues.map(issue => <article key={issue.issue_id}><h3>{issue.label}</h3><button onClick={() => void reanalyzeIssue(issue.issue_id)}>Review this issue again</button></article>) : <p>No issues recorded. Review a document to begin.</p>}</details><RunHistory /></section>}
    {tab === "matrix" && matrixError && <p role="alert">Claim review could not be loaded. <button onClick={() => void refresh().catch(() => setError("Could not refresh review."))}>Retry review</button></p>}
    {tab === "matrix" && !matrixError && <><button disabled={!findings.length} onClick={exportReview}>Export review report</button><EvidenceMatrixPanel matterId={matterId} rows={matrix} openClaim={openClaim} alerts={alerts} targets={monitoring?.targets ?? []} reviewedClaims={claims.filter(c => c.reviewed).map(c => c.claim_id)} caseNames={Object.fromEntries(findings.flatMap(f => f.evidence.map(e => [e.result.case_id, e.result.case_name])))} />{report && <section aria-label="Review report"><h3>Review report</h3><p>Review this draft before sharing it.</p><label>Report text<textarea readOnly value={report} /></label><a download="lextrace-review.md" href={"data:text/markdown;charset=utf-8," + encodeURIComponent(report)}>Download Markdown report</a><button onClick={() => setReport("")}>Close report</button></section>}</>}

    {tab === "documents" && <section className="workspace"><h2>Documents</h2><p>Uploaded files stay in private local runtime storage. Scanned PDFs require OCR and are not analyzed.</p>
      <label className="upload-control">Upload PDF, DOCX, TXT, or Markdown<input type="file" accept=".pdf,.docx,.txt,.md" disabled={busy} onChange={upload} /></label>
      <div className="matter-cards">{documents.map((doc) => <article key={doc.document_id}><h3>{doc.filename}</h3><p>{doc.document_type.toUpperCase()} · {doc.page_count ? `${doc.page_count} pages` : "Pages unavailable"} · {label(doc.ingestion_status)} · {claims.some(c => c.document_id === doc.document_id) ? "Analysis available in Review" : "Not yet analyzed"}</p>{doc.analysis_warnings?.map((warning) => <p key={warning} className="notice">{warning}</p>)}<button onClick={() => setPreviewId(doc.document_id)}>Preview extracted text</button><button disabled={doc.ingestion_status !== "READY" || (job?.status === "running" || job?.status === "queued")} onClick={() => void analyze(doc.document_id)}>Review document</button></article>)}</div>
      {previewId && detail?.document.document_id === previewId && <section><h3>Extracted document text</h3><p>Check that the text is readable before starting a review.</p><div className="source-viewer">{detail.text}</div><button onClick={() => setPreviewId(null)}>Close preview</button></section>}
      {!documents.length && <p>Upload your brief to begin. Then choose Review document.</p>}
    </section>}

    {selectedClaim && <section ref={panel} tabIndex={-1} className="claim-detail" aria-label="Claim detail"><div className="claim-detail-head"><div><p className="eyebrow">Review claim</p><h2>{selectedClaim.normalized_proposition}</h2></div><button className="close-button" onClick={() => setSelectedId(null)}>Close</button></div>
      <h3>Exact document source</h3><p>{documents.find((doc) => doc.document_id === selectedClaim.document_id)?.filename} {selectedClaim.span.page ? `· page ${selectedClaim.span.page}` : ""} · characters {selectedClaim.span.start}–{selectedClaim.span.end}</p>
      {detail?.document.document_id === selectedClaim.document_id && <div className="source-viewer">{detail.text.slice(0, selectedClaim.span.start)}<mark>{detail.text.slice(selectedClaim.span.start, selectedClaim.span.end)}</mark>{detail.text.slice(selectedClaim.span.end)}</div>}
      <section><h3>Your review</h3><p>{selectedClaim.reviewed ? "Reviewed by you" : "Not reviewed by you"}. This is separate from the model assessment.</p><label>Reviewer notes<textarea maxLength={4000} value={reviewNotes} onChange={event => setReviewNotes(event.target.value)} /></label><button onClick={() => void saveReview(true)}>Save as reviewed</button><button onClick={() => void saveReview(false)}>Save notes · needs review</button></section><h3>Normalized proposition</h3><textarea value={proposition} disabled={!!selectedClaim.wording_locked} onChange={(event) => setProposition(event.target.value)} /><div className="review-actions"><button disabled={!!selectedClaim.wording_locked} onClick={() => void saveClaim()}>Save edit</button><button onClick={() => void toggleLock()}>{selectedClaim.wording_locked ? "Unlock wording" : "Lock wording"}</button><button onClick={() => void markIrrelevant()}>{selectedClaim.irrelevant ? "Restore claim" : "Mark irrelevant"}</button><button onClick={() => void reanalyze()}>Re-analyze this claim</button><button onClick={() => void watch("CLAIM", selectedClaim.claim_id)}>{watching("CLAIM", selectedClaim.claim_id) ? "Watching claim · pause" : "Watch this claim"}</button></div>
      {selectedFinding ? <><p className="notice">Proposed model assessment. Source matching verifies provenance, not the legal interpretation. Compare the passages and record your review.</p><h3>Why this assessment?</h3><p><span className={`finding-status ${selectedFinding.vulnerability.toLowerCase()}`}>{label(selectedFinding.vulnerability)}</span> {selectedFinding.explanation}</p><p>Verification: {label(selectedFinding.verification_status)} · Research: {label(selectedFinding.research_coverage)}</p>{selectedFinding.unresolved_citation_ids.length > 0 && <p className="notice">{selectedFinding.unresolved_citation_ids.length} unresolved citation reference(s). Additional research is needed.</p>}
        <h3>Exact case-law passages</h3>{selectedFinding.evidence.map((evidence) => <div key={evidence.evidence_id}>{!selectedFinding.cited_authorities.some((link) => link.evidence_id === evidence.evidence_id && link.removed) && <EvidenceCard evidence={evidence} />}{selectedFinding.cited_authorities.filter((link) => link.evidence_id === evidence.evidence_id).map((link) => <div key={`${link.case_id}-${link.relation}-${link.evidence_id}`} className="review-actions"><span>{link.removed ? "Removed by reviewer" : link.relation}</span><button onClick={() => void editAuthority(link.case_id, link.evidence_id, { pinned: !link.pinned })}>{link.pinned ? "Unpin" : "Pin"}</button><button onClick={() => void editAuthority(link.case_id, link.evidence_id, { removed: !link.removed })}>{link.removed ? "Restore" : "Remove suggestion"}</button></div>)}</div>)}<details><summary>Investigate this claim and find counterarguments</summary><DeepResearchPanel matterId={matterId} claimId={selectedClaim.claim_id} onUpdate={refresh} /></details><details><summary>Authority history and how the rule developed</summary><PrecedentPanel matterId={matterId} claimId={selectedClaim.claim_id} /></details></> : <p>This claim needs re-analysis after the latest edit.</p>}
    </section>}
  </>;
}

function EvidenceCard({ evidence }: { evidence: Evidence }) {
  const item = evidence.result;
  return <article className="evidence-card"><p className="eyebrow">{evidence.role.replaceAll("_", " ")} · {item.court} · {item.date_filed ?? "date unknown"}</p><h3>{item.case_name}</h3><p>{item.reporter_citations?.join(", ")}</p><blockquote>{item.relevant_passage.text}</blockquote><p className="evidence-meta">Case {item.case_id} · Opinion {item.relevant_passage.opinion_id} · Passage {item.relevant_passage.passage_id}</p><a href={item.source_url} target="_blank" rel="noreferrer">Open CourtListener source ↗</a></article>;
}
