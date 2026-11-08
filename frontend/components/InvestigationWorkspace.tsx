"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import {
  ApprovedVersion, decisionStorageKey, investigationBase, InvestigationDecision,
  InvestigationView, Json, PublishedSnapshot,
} from "@/lib/investigations";
import { DocxDownload } from "./DocxDownload";
import { InvestigationData, InvestigationReview } from "./InvestigationReview";

export function investigationError(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.status) {
      case 401: return "Authentication required. Set the operator credential for this browser tab, then refresh.";
      case 403: return "This operator cannot access this workspace. Check the configured operator binding.";
      case 404: return "This investigation or artifact is unavailable in this matter.";
      case 409: return "The investigation changed, its inputs are stale, or the runtime is busy. The server view will be refreshed; no new decision is submitted automatically.";
      case 422: return "The review request is invalid. Check the record decisions and corrections before preparing a new request.";
      case 503: return "The investigation service is unavailable. Refresh to reconcile server progress, or retry the identical submitted request.";
    }
  }
  return "The request could not be confirmed. An investigation may already have started. Check Existing investigations before creating another run. A submitted request can be retried unchanged.";
}

export function InvestigationWorkspace({ matterId, threadId }: {
  matterId: string; threadId?: string;
}) {
  const router = useRouter();
  const base = investigationBase(matterId);
  const [runs, setRuns] = useState<InvestigationView[]>([]);
  const [run, setRun] = useState<InvestigationView | null>(null);
  const [eligibleVersions, setEligibleVersions] = useState<ApprovedVersion[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const sending = useRef(false);
  const storageKey = threadId ? decisionStorageKey(matterId, threadId) : `lextrace.investigationStart:${encodeURIComponent(matterId)}`;

  const refresh = useCallback(async () => {
    if (threadId) setRun(await api<InvestigationView>(`${base}/${encodeURIComponent(threadId)}`));
    else {
      const [nextRuns, versions] = await Promise.all([
        api<InvestigationView[]>(base), api<ApprovedVersion[]>(`${base}/eligible-versions`),
      ]);
      setRuns(nextRuns); setEligibleVersions(versions); setSelected([]);
    }
  }, [base, threadId]);

  useEffect(() => {
    let active = true;
    void Promise.resolve().then(async () => {
      try {
        const saved = storageKey ? window.sessionStorage.getItem(storageKey) : null;
        if (active) setPending(saved);
      } catch { if (active) setError("Browser retry storage is unavailable. Keep this page open while a review request is unresolved."); }
      try {
        if (threadId) {
          const next = await api<InvestigationView>(`${base}/${encodeURIComponent(threadId)}`);
          if (active) setRun(next);
        } else {
          const [next, versions] = await Promise.all([
            api<InvestigationView[]>(base), api<ApprovedVersion[]>(`${base}/eligible-versions`),
          ]);
          if (active) { setRuns(next); setEligibleVersions(versions); setSelected([]); }
        }
      } catch (failure) { if (active) setError(investigationError(failure)); }
      finally { if (active) setLoaded(true); }
    });
    return () => { active = false; };
  }, [base, storageKey, threadId]);

  async function reload() {
    if (sending.current) return;
    setBusy(true); setError("");
    try { await refresh(); } catch (failure) {
      setError(investigationError(failure));
      // A failed read cannot leave an older review available for approval.
      if (threadId) setRun(null); else { setRuns([]); setEligibleVersions([]); setSelected([]); }
    }
    finally { setBusy(false); }
  }
  async function start() {
    if (sending.current || pending || !selected.length) return;
    // Use only server-listed active manifests; M7 revalidates them at start.
    const versions = eligibleVersions.filter((v) => selected.includes(JSON.stringify(v)));
    if (!versions.length || versions.length > 100 || new Set(versions.map((v) => v.document_id)).size !== versions.length) {
      setError("Select at most 100 current versions, with one version per document."); return;
    }
    const body = JSON.stringify({ request_id: crypto.randomUUID(), approved_versions: versions });
    try { window.sessionStorage.setItem(storageKey, body); setPending(body); }
    catch { setError("Browser retry storage is unavailable. Starting is blocked until the request can be saved safely."); return; }
    await transmitStart(body);
  }
  async function transmitStart(body: string) {
    if (sending.current) return;
    sending.current = true; setBusy(true); setError("");
    try {
      const next = await api<InvestigationView>(base, { method: "POST", body });
      window.sessionStorage.removeItem(storageKey); setPending(null);
      router.push(`${base}/${encodeURIComponent(next.thread_id)}`);
    } catch (failure) {
      setError(failure instanceof ApiError && failure.status === 409
        ? "The start is busy, conflicting, or its sources are no longer current. Refresh server status; retry only the saved identical request."
        : failure instanceof ApiError && failure.status === 422
          ? "The saved source request is invalid. Review server status before discarding it."
          : investigationError(failure));
      await refresh().catch(() => undefined);
    }
    finally { sending.current = false; setBusy(false); }
  }

  async function transmit(body: string) {
    if (!threadId || sending.current) return;
    sending.current = true; setBusy(true); setError("");
    try {
      const next = await api<InvestigationView>(`${base}/${encodeURIComponent(threadId)}/decisions`, { method: "POST", body });
      setRun(next);
      // Successful server acknowledgement is the only automatic discard point.
      setPending(null);
      if (storageKey) window.sessionStorage.removeItem(storageKey);
    } catch (failure) {
      const message = investigationError(failure);
      // Reconciliation is a read, never an automatic changed resubmission.
      try { await refresh(); setError(message); }
      catch { setError(`${message} The current server view could not be refreshed; approval actions remain locked.`); setRun(null); }
    } finally { sending.current = false; setBusy(false); }
  }
  function submit(decision: InvestigationDecision) {
    if (pending || sending.current) return;
    const body = JSON.stringify(decision);
    // Persist once before dispatch: IDs, timestamps, token, and ordered commands
    // remain byte-identical across retries, including a same-tab page refresh.
    try { if (storageKey) window.sessionStorage.setItem(storageKey, body); }
    catch { setError("The review was not sent because browser retry storage is unavailable."); return; }
    setPending(body);
    void transmit(body);
  }
  function discard() {
    if (busy || !window.confirm("Discard the saved request? This does not undo a server decision. Refresh before preparing another review.")) return;
    try { if (storageKey) window.sessionStorage.removeItem(storageKey); }
    catch { setError("The saved request could not be discarded."); return; }
    setPending(null);
    // Clear local drafts and require a fresh server read before another decision.
    setRun(null); void reload();
  }

  return <section aria-label="Investigation workspace" className="investigation-workspace">
    <div className="matter-heading"><div><p className="eyebrow">Matter investigations</p><h1>{threadId ? "Investigation review" : "Investigations"}</h1></div><Link href={`/matters/${encodeURIComponent(matterId)}`}>Back to matter →</Link></div>
    <p>Review source accounts and investigative hypotheses against exact evidence. A signed snapshot records human approval, not proof of a contradiction.</p>
    {error && <p role="alert" className="notice">{error}</p>}
    {!loaded && <p role="status">Loading investigations…</p>}
    <div className="review-actions"><button disabled={busy} onClick={() => void reload()}>Refresh server status</button>{threadId && <><Link href={base}>All investigations →</Link><Link href={`${base}/${encodeURIComponent(threadId)}/deposition`}>Deposition preparation →</Link></>}</div>
    {threadId && <DocxDownload path={`${base}/${encodeURIComponent(threadId)}/export.docx`} label="Download investigation report" filename="lextrace-investigation-report.docx" disabled={busy || run?.stage !== "APPROVED" || !run.snapshot_id} reason="A current signed investigation snapshot is required for Word export." />}
    {!threadId && <><h2>Existing investigations</h2><div className="matter-cards">{runs.map((item) => <Link key={item.thread_id} href={`${base}/${encodeURIComponent(item.thread_id)}`}><strong>Run {item.run_id}</strong><span>{item.stage} · {item.checkpoint ? `${item.checkpoint} review` : "No active review"}</span></Link>)}</div>{loaded && runs.length === 0 && <p>No investigations are available.</p>}
      <section className="workspace"><h2>Start investigation</h2><p>Select eligible active document versions. READY sources are selectable; this does not imply human approval of their contents. Each new start has a durable request identity. An uncertain start can be retried unchanged after refresh.</p>
        {loaded && eligibleVersions.length === 0 && <p className="notice">No eligible document versions are available. Upload a readable document with READY status and a registered active version, then refresh.</p>}
        {eligibleVersions.map((version) => { const key = JSON.stringify(version); return <label className="investigation-check" key={key}><input type="checkbox" disabled={busy || !!pending} checked={selected.includes(key)} onChange={(e) => setSelected(e.target.checked ? [...selected, key] : selected.filter((v) => v !== key))} />Document {version.document_id} · version {version.version_id}</label>; })}
        <button disabled={busy || !!pending || !loaded || selected.length === 0} onClick={() => void start()}>Start investigation</button>
      </section></>}
    {!threadId && pending && <section className="notice" aria-label="Saved start request"><h2>Start awaiting acknowledgement</h2><p>The exact source selection and request identity are saved in this tab. Retry the same request to recover its existing investigation. Discarding it does not undo server work.</p><div className="review-actions"><button disabled={busy} onClick={() => void transmitStart(pending)}>Retry identical start request</button><button disabled={busy} onClick={discard}>Discard saved request</button></div></section>}
    {threadId && pending && <section className="notice" aria-label="Saved review request"><h2>Submitted review awaiting acknowledgement</h2><p>The exact submitted request is saved in this tab for retry. Server progress is authoritative; review controls stay locked until this request is acknowledged or explicitly discarded.</p><div className="review-actions"><button disabled={busy} onClick={() => void transmit(pending)}>Retry identical review request</button><button disabled={busy} onClick={discard}>Discard saved request</button></div></section>}
    {threadId && run && <><h2>Run {run.run_id}</h2><p role="status">{run.stage} · {run.checkpoint ? `${run.checkpoint} review checkpoint` : "No active review checkpoint"}</p><p>Coverage: {run.coverage.join(", ") || "Not reported"}</p>{run.errors.length > 0 && <p className="notice">Run diagnostics: {run.errors.join(", ")}</p>}
      {run.stage === "WAITING_REVIEW" && run.checkpoint && run.token && run.digest && run.review && <InvestigationReview key={`${run.run_id}:${run.token}:${pending ? "locked" : "open"}`} run={run} disabled={busy || !!pending} onDecision={submit} />}
      {run.stage === "APPROVED" && run.snapshot_id && <SignedInvestigation key={run.snapshot_id} matterId={matterId} threadId={threadId} snapshotId={run.snapshot_id} />}
    </>}
  </section>;
}

function SignedInvestigation({ matterId, threadId, snapshotId }: { matterId: string; threadId: string; snapshotId: string }) {
  const path = `${investigationBase(matterId)}/${encodeURIComponent(threadId)}/snapshots/${encodeURIComponent(snapshotId)}`;
  const [snapshot, setSnapshot] = useState<PublishedSnapshot | null>(null);
  const [artifact, setArtifact] = useState<{ id: string; value: Json } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    void api<PublishedSnapshot>(path).then((next) => { if (active) setSnapshot(next); })
      .catch((failure) => { if (active) setError(investigationError(failure)); });
    return () => { active = false; };
  }, [path]);
  async function readArtifact(id: string) {
    setBusy(true); setError(""); setArtifact(null);
    try { setArtifact({ id, value: await api<Json>(`${path}/artifacts/${encodeURIComponent(id)}`) }); }
    catch (failure) { setError(investigationError(failure)); }
    finally { setBusy(false); }
  }
  return <section className="precedent-panel" aria-label="Published investigation"><h3>Published hypothesis snapshot</h3>{error && <p role="alert" className="notice">{error}</p>}
    {snapshot && <><p>{snapshot.meaning.replaceAll("_", " ")}</p><p>Signed by {snapshot.reviewer_id}</p><p className="evidence-meta">Snapshot {snapshot.snapshot_id}</p><details><summary>Signed snapshot metadata</summary><InvestigationData value={snapshot as unknown as Json} /></details><div className="review-actions"><button disabled={busy} onClick={() => void readArtifact(snapshot.chronology_id)}>Read signed chronology</button>{snapshot.finding_ids.map((id, i) => <button key={id} disabled={busy} onClick={() => void readArtifact(id)}>Read signed finding {i + 1}</button>)}</div></>}
    {artifact && <article className="evidence-card" aria-label="Signed artifact"><p className="evidence-meta">Signed artifact {artifact.id}</p><InvestigationData value={artifact.value} /></article>}
  </section>;
}
