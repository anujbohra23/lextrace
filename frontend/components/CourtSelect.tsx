"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
const names: Record<string,string> = {ca2:"U.S. Court of Appeals — Second Circuit",scotus:"U.S. Supreme Court",ca5:"U.S. Court of Appeals — Fifth Circuit",ca11:"U.S. Court of Appeals — Eleventh Circuit"};
export function CourtSelect({value,onChange}: {value: string; onChange: (value: string) => void}) {
  const [courts,setCourts] = useState<string[]>([]);
  useEffect(() => { let mounted = true; api<{courts:string[]}>("/corpus/coverage").then(data => { if (mounted && Array.isArray(data.courts)) setCourts(data.courts); }).catch(() => {}); return () => { mounted = false; }; }, []);
  return <label>Court scope (optional)<select value={value} onChange={event => onChange(event.target.value)}><option value="">All courts in the collection</option>{courts.map(court => <option key={court} value={court}>{names[court] ?? court}</option>)}</select><small>Leave unrestricted to include other courts and Supreme Court decisions.</small></label>;
}
