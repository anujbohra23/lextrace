"use client";

import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Matter } from "@/lib/matter";

export function MatterList() {
  const [matters, setMatters] = useState<Matter[]>([]);
  const [name, setName] = useState("");
  const [court, setCourt] = useState("");
  const [error, setError] = useState(false);

  useEffect(() => { void api<Matter[]>("/matters").then(setMatters).catch(() => setError(true)); }, []);

  async function create(event: FormEvent) {
    event.preventDefault();
    setError(false);
    try {
      const matter = await api<Matter>("/matters", {
        method: "POST", body: JSON.stringify({ name, court: court || null }),
      });
      setMatters((previous) => [...previous, matter]);
      setName(""); setCourt("");
    } catch { setError(true); }
  }

  return <>
    <section className="hero">
      <p className="eyebrow">Litigation Argument Intelligence</p>
      <h1>Trace every argument. Test every authority.</h1>
      <p>Find the weakness before opposing counsel does. Upload a brief, inspect its claims, and trace every finding to document text and case-law passages.</p>
    </section>
    <section className="matter-layout">
      <form onSubmit={create}>
        <h2>Create a matter</h2>
        <label>Matter name<input required maxLength={200} value={name} onChange={(event) => setName(event.target.value)} placeholder="Smith v. Acme" /></label>
        <label>Court or forum<input value={court} onChange={(event) => setCourt(event.target.value)} placeholder="S.D.N.Y." /></label>
        <button>Create matter</button>
      </form>
      <div><h2>Your matters</h2>
        {error && <p role="alert">Matter service is unavailable. Check the backend.</p>}
        {matters.length === 0 && <p>No matters yet.</p>}
        <div className="matter-cards">{matters.map((matter) =>
          <Link key={matter.matter_id} href={`/matters/${matter.matter_id}`}>
            <strong>{matter.name}</strong><span>{matter.court || matter.jurisdiction || "Forum unspecified"}</span>
          </Link>)}</div>
      </div>
    </section>
  </>;
}
