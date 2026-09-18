"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
type Coverage = { case_count: number; courts: string[]; earliest_date: string | null; latest_date: string | null };
export function CorpusCoverage() {
  const [coverage, setCoverage] = useState<Coverage | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => { let active = true; api<Coverage>("/corpus/coverage").then(value => { if (active) { if (typeof value.case_count === "number" && Array.isArray(value.courts)) setCoverage(value); else setFailed(true); } }).catch(() => { if (active) setFailed(true); }); return () => { active = false; }; }, []);
  return <aside className="coverage-note" aria-label="Available sources"><strong>Available sources</strong><p>{coverage ? `${coverage.case_count} locally indexed cases · ${coverage.courts.join(", ")} · ${coverage.earliest_date ?? "unknown date"} to ${coverage.latest_date ?? "unknown date"}.` : failed ? "Source coverage could not be loaded. Check the local index before relying on results." : "Checking source coverage…"} This collection is not comprehensive or continuously updated. No result does not mean no relevant law exists.</p></aside>;
}
