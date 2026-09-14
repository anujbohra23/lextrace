"use client";

import { useMemo, useState } from "react";
import { API_URL } from "@/lib/api";
import { MatrixRow } from "@/lib/deepResearch";
import { MatterAlert, MonitoringTarget } from "@/lib/monitoring";

type Filters = {
  issue: string; status: string; authority: string; citation: string;
  coverage: string; severity: string; document: string; unresolved: boolean;
};
const EMPTY: Filters = {
  issue: "ALL", status: "ALL", authority: "ALL", citation: "ALL",
  coverage: "ALL", severity: "ALL", document: "ALL", unresolved: false,
};
const coverageOrder: Record<string, number> = {
  INSUFFICIENT: 0, WEAK: 1, UNKNOWN: 2, PARTIAL: 3, SUFFICIENT: 4,
};
const severityOrder: Record<string, number> = {
  CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3,
};

export function EvidenceMatrixPanel({
  matterId, rows, openClaim, alerts = [], targets = [],
}: {
  matterId: string; rows: MatrixRow[]; openClaim: (claimId: string) => void;
  alerts?: MatterAlert[]; targets?: MonitoringTarget[];
}) {
  const [filters, setFilters] = useState<Filters>(EMPTY);
  const [sort, setSort] = useState("vulnerability");
  const [showChange, setShowChange] = useState(false);
  const options = (field: keyof MatrixRow): string[] => [
    ...new Set(rows.map((row) => row[field]).filter((value): value is string => typeof value === "string")),
  ].sort();
  const visible = useMemo(() => {
    const filtered = rows.filter((row) =>
      (filters.issue === "ALL" || row.issue_id === filters.issue) &&
      (filters.status === "ALL" || row.argument_status === filters.status) &&
      (filters.authority === "ALL" || row.authority_category === filters.authority) &&
      (filters.citation === "ALL" || row.citation_support === filters.citation) &&
      (filters.coverage === "ALL" || row.coverage === filters.coverage) &&
      (filters.severity === "ALL" || row.attack_severity === filters.severity) &&
      (filters.document === "ALL" || row.document_id === filters.document) &&
      (!filters.unresolved || row.gaps.length > 0)
    );
    return filtered.sort((a, b) => {
      if (sort === "coverage") return (coverageOrder[a.coverage] ?? 9) - (coverageOrder[b.coverage] ?? 9) || a.claim_id.localeCompare(b.claim_id);
      if (sort === "issue") return a.issue.localeCompare(b.issue) || a.claim_id.localeCompare(b.claim_id);
      if (sort === "authority") return a.authority_category.localeCompare(b.authority_category) || a.claim_id.localeCompare(b.claim_id);
      if (sort === "importance") return ({ high: 0, medium: 1, low: 2 }[a.importance] ?? 9) - ({ high: 0, medium: 1, low: 2 }[b.importance] ?? 9);
      return (severityOrder[a.attack_severity ?? ""] ?? 9) - (severityOrder[b.attack_severity ?? ""] ?? 9) || a.claim_id.localeCompare(b.claim_id);
    });
  }, [filters, rows, sort]);

  function select(label: string, key: keyof Filters, values: string[]) {
    return <label>{label}<select value={String(filters[key])} onChange={(event) => setFilters({ ...filters, [key]: event.target.value })}>
      <option value="ALL">All</option>{values.map((value) => <option key={value} value={value}>{value}</option>)}
    </select></label>;
  }
  return <section className="workspace" aria-label="Evidence Matrix v2">
    <h2>Evidence Matrix v2</h2><p>Claim support, research coverage, and attack severity are separate signals. Select a claim for source passages and research history.</p>
    <div className="matrix-controls">
      {select("Issue", "issue", options("issue_id"))}
      {select("Claim status", "status", options("argument_status"))}
      {select("Authority", "authority", options("authority_category"))}
      {select("Citation support", "citation", options("citation_support"))}
      {select("Coverage", "coverage", options("coverage"))}
      {select("Attack severity", "severity", options("attack_severity"))}
      {select("Document", "document", options("document_id"))}
      <label><input type="checkbox" checked={filters.unresolved} onChange={(event) => setFilters({ ...filters, unresolved: event.target.checked })} /> Unresolved gaps only</label>
      <label>Sort<select value={sort} onChange={(event) => setSort(event.target.value)}><option value="vulnerability">Vulnerability</option><option value="coverage">Coverage</option><option value="issue">Issue</option><option value="authority">Authority</option><option value="importance">Importance</option></select></label>
      <label><input type="checkbox" checked={showChange} onChange={(event) => setShowChange(event.target.checked)} /> Show change columns</label>
      <a href={`${API_URL}/matters/${matterId}/evidence-matrix.csv`}>Export CSV</a>
    </div>
    <div className="matrix-scroll"><table className="evidence-matrix"><thead><tr><th>Issue</th><th>Claim</th><th>Source</th><th>Matter evidence</th><th>Cited authority</th><th>Citation support</th><th>Authority status</th><th>Strongest support</th><th>Strongest counter</th><th>Later treatment</th><th>Doctrine state</th><th>Coverage</th><th>Red Team</th><th>Argument status</th><th>Gaps</th>{showChange && <><th>Last checked</th><th>New authority</th><th>Change impact</th><th>Alert status</th></>}</tr></thead><tbody>
      {visible.map((row) => { const latest = alerts.find((alert) => alert.claim_id === row.claim_id); const target = targets.find((item) => item.target_type === "CLAIM" && item.target_reference_id === row.claim_id); return <tr key={row.claim_id}><td>{row.issue}</td><td><button className="link-button" onClick={() => openClaim(row.claim_id)}>{row.claim}</button></td><td>{row.document_name} · {row.source_start}–{row.source_end}</td><td>{row.matter_evidence_ids?.length ? `${row.matter_evidence_ids.length} linked facts` : "None recorded"}</td><td>{row.cited_case_ids.join(", ") || "None"}</td><td>{row.citation_support}</td><td>{row.authority_category}</td><td>{row.strongest_support_case_id ?? "None"}</td><td>{row.strongest_counter_case_id ?? "None"}</td><td>{row.later_treatment?.join(", ") || "Not reviewed"}</td><td>{row.doctrine_state}</td><td>{row.coverage}</td><td>{row.attack_severity ?? "None verified"}</td><td>{row.argument_status}</td><td>{row.gaps.join(", ") || "None recorded"}</td>{showChange && <><td>{target?.last_checked_at?.slice(0, 10) ?? "Not checked"}</td><td>{latest?.new_case_id ?? "None"}</td><td>{latest?.title ?? "No material change"}</td><td>{latest?.review_state ?? "None"}</td></>}</tr>; })}
    </tbody></table></div>
    {visible.length === 0 && <p>No claims match these filters.</p>}
  </section>;
}
