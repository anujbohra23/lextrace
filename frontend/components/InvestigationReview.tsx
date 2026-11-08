"use client";

import { FormEvent, useState } from "react";
import {
  DateCorrection, EntityLink, EvidenceDecision, InvestigationDecision, InvestigationView,
  Json, recordLabel, ReviewRecord, reviewTarget, targetKey,
} from "@/lib/investigations";

/** Render backend evidence as text; never interpret source text as HTML. */
export function InvestigationData({ value }: { value: Json }) {
  if (value === null) return <span>Not provided</span>;
  if (typeof value !== "object") return <span>{String(value)}</span>;
  if (Array.isArray(value)) return value.length
    ? <ul>{value.map((item, i) => <li key={i}><InvestigationData value={item} /></li>)}</ul>
    : <span>None recorded</span>;
  return <dl className="investigation-data">{Object.entries(value).map(([key, item]) => <div key={key}>
    <dt>{key.replaceAll("_", " ")}</dt><dd>{key === "exact_quote" && typeof item === "string"
      ? <blockquote>{item}</blockquote> : <InvestigationData value={item} />}</dd>
  </div>)}</dl>;
}

type Edit = {
  decision: "" | EvidenceDecision["decision"]; explanation: string;
  correctionKind: "" | DateCorrection["kind"]; earliest: string; latest: string;
  precision: DateCorrection["precision"]; entityId: string;
};
const emptyEdit: Edit = { decision: "", explanation: "", correctionKind: "", earliest: "", latest: "", precision: "day", entityId: "" };

export function InvestigationReview({ run, disabled, onDecision }: {
  run: InvestigationView; disabled: boolean; onDecision: (decision: InvestigationDecision) => void;
}) {
  const [edits, setEdits] = useState<Record<string, Edit>>({});
  const [links, setLinks] = useState<Record<string, string[]>>({});
  const [confirmed, setConfirmed] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [error, setError] = useState("");
  const records = run.review?.records ?? [];
  const incomplete = run.coverage.some((item) => item !== "COMPLETE");
  const gate = run.checkpoint;
  const mentions = records.filter((record) => reviewTarget(record).kind === "mention");

  function change(record: ReviewRecord, update: Partial<Edit>) {
    const key = targetKey(record);
    setEdits({ ...edits, [key]: { ...(edits[key] ?? emptyEdit), ...update } });
  }
  function submit(decision: "APPROVE" | "REJECT") {
    if (!gate || !run.token || !run.digest || disabled) return;
    if (decision === "APPROVE" && (!confirmed || (incomplete && !acknowledged))) return;
    try {
      const commands: EvidenceDecision[] = [];
      const entityLinks: EntityLink[] = [];
      if (gate === "evidence" && decision === "APPROVE") {
        for (const record of records) {
          const key = targetKey(record);
          const edit = edits[key] ?? emptyEdit;
          const target = reviewTarget(record);
          if (edit.decision) {
            const command: EvidenceDecision = {
              decision_id: crypto.randomUUID(), timestamp: new Date().toISOString(),
              target, expected_revision: record.revision, decision: edit.decision,
            };
            if (edit.explanation.trim()) command.explanation = edit.explanation.trim();
            if (edit.decision === "EDIT") {
              if (target.kind === "mention") {
                if (!edit.entityId.trim()) throw new Error("Enter an entity identifier for every edited mention.");
                command.resolved_entity_id = edit.entityId.trim();
              } else {
                if (!edit.correctionKind) throw new Error("Choose a date correction for every edited account.");
                const unknown = edit.correctionKind === "unknown";
                const exact = edit.correctionKind === "exact";
                const latest = exact ? edit.earliest : edit.latest;
                if (!unknown && (!edit.earliest || !latest || edit.earliest > latest)) throw new Error("Date corrections require ordered start and end dates.");
                command.date_correction = { kind: edit.correctionKind,
                  earliest: unknown ? null : edit.earliest, latest: unknown ? null : latest,
                  precision: unknown ? "unknown" : exact ? "day" : edit.precision };
              }
            }
            commands.push(command);
          }
          const selected = links[key] ?? [];
          if (target.kind !== "mention" && selected.length) {
            entityLinks.push({ account: target, mentions: mentions.filter((item) => selected.includes(targetKey(item))).map(reviewTarget) });
          }
        }
      }
      // The parent freezes and retains this complete envelope before any request.
      onDecision({ run_id: run.run_id, gate, token: run.token, digest: run.digest,
        decision, acknowledge_incomplete_coverage: acknowledged,
        entity_links: entityLinks, evidence_decisions: commands });
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Check your review edits."); }
  }
  function approve(event: FormEvent) { event.preventDefault(); submit("APPROVE"); }

  return <form onSubmit={approve} aria-label={`${gate} review`} className="investigation-review">
    <h3>{gate === "evidence" ? "Evidence review" : "Findings review"}</h3>
    <p>Source accounts remain separate. Acceptance does not establish factual truth; findings are investigative hypotheses.</p>
    {error && <p role="alert" className="notice">{error}</p>}
    <fieldset disabled={disabled}>
      {gate === "evidence" && records.map((record) => {
        const key = targetKey(record); const target = reviewTarget(record);
        const edit = edits[key] ?? emptyEdit;
        return <article className="evidence-card" key={key}>
          <h4>{recordLabel(record)}</h4><p>{target.kind} · {record.review_state} · revision {record.revision}</p>
          <p className="evidence-meta">Document {target.document_id} · version {target.version_id} · record {target.record_id}</p>
          {record.original.evidence_references.map((ref, i) => <div key={i}><blockquote>{ref.exact_quote}</blockquote><p className="evidence-meta">Section {ref.section_id} · characters {ref.start}–{ref.end}{ref.page ? ` · page ${ref.page}` : ""}</p></div>)}
          <details><summary>Original record details</summary><InvestigationData value={Object.fromEntries(Object.entries(record.original).filter(([key]) => key !== "evidence_references")) as Json} /></details>
          <details><summary>Current corrections and identity</summary><InvestigationData value={record.date_correction ?? null} /><p>{record.resolved_entity_id ?? "No resolved entity"}</p></details>
          <label>Decision for {target.record_id}<select value={edit.decision} onChange={(e) => change(record, { decision: e.target.value as Edit["decision"] })}>
            <option value="">Keep current review</option><option value="ACCEPT">Accept</option><option value="REJECT">Reject record</option><option value="DEFER">Defer</option><option value="EDIT">Edit interpretation</option>
          </select></label>
          {edit.decision && <label>Explanation for {target.record_id}<input value={edit.explanation} onChange={(e) => change(record, { explanation: e.target.value })} /></label>}
          {edit.decision === "EDIT" && (target.kind === "mention"
            ? <label>Resolved entity for {target.record_id}<input value={edit.entityId} onChange={(e) => change(record, { entityId: e.target.value })} /></label>
            : <div className="matrix-controls"><label>Date correction for {target.record_id}<select value={edit.correctionKind} onChange={(e) => change(record, { correctionKind: e.target.value as Edit["correctionKind"] })}><option value="">Choose correction</option>{["exact", "approximate", "interval", "unknown"].map((value) => <option key={value}>{value}</option>)}</select></label>
              {edit.correctionKind && edit.correctionKind !== "unknown" && <><label>Start date for {target.record_id}<input type="date" value={edit.earliest} onChange={(e) => change(record, { earliest: e.target.value })} /></label>{edit.correctionKind !== "exact" && <><label>End date for {target.record_id}<input type="date" value={edit.latest} onChange={(e) => change(record, { latest: e.target.value })} /></label><label>Precision for {target.record_id}<select value={edit.precision} onChange={(e) => change(record, { precision: e.target.value as Edit["precision"] })}>{["day", "month", "year", "unknown"].map((v) => <option key={v}>{v}</option>)}</select></label></>}</>}
            </div>)}
          {target.kind !== "mention" && <fieldset><legend>Entity mentions linked to {target.record_id}</legend><p>Links are reviewer interpretations, not proof of identity.</p>{mentions.map((mention) => {
            const id = targetKey(mention); const chosen = links[key] ?? [];
            return <label className="investigation-check" key={id}><input type="checkbox" checked={chosen.includes(id)} onChange={(e) => setLinks({ ...links, [key]: e.target.checked ? [...chosen, id] : chosen.filter((v) => v !== id) })} />{recordLabel(mention)} · {mention.original.document_id}</label>;
          })}{mentions.length === 0 && <p>No mention candidates.</p>}</fieldset>}
        </article>;
      })}
      {run.review?.chronology && <details open><summary>Chronology · overlapping dates do not establish sequence</summary><InvestigationData value={run.review.chronology} /></details>}
      {gate === "findings" && <><h4>Findings · unverified investigative hypotheses</h4><InvestigationData value={run.review?.findings ?? null} /></>}
      <p>Coverage: {run.coverage.join(", ") || "Not reported"}</p>
      {incomplete && <label className="investigation-check"><input type="checkbox" checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} />I acknowledge incomplete coverage for this review.</label>}
      <label className="investigation-check"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />I confirm approval of this {gate} review{gate === "findings" ? " and publication of the hypothesis snapshot" : ""}.</label>
      <div className="review-actions"><button type="submit" disabled={!confirmed || (incomplete && !acknowledged)}>Approve {gate} review</button><button type="button" onClick={() => { if (window.confirm("Reject this investigation? The current workflow will be blocked.")) submit("REJECT"); }}>Reject investigation</button></div>
    </fieldset>
  </form>;
}
