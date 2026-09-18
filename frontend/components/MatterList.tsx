"use client";

import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Matter } from "@/lib/matter";

export function MatterList() {
  const [matters, setMatters] = useState<Matter[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState("");
  const [court, setCourt] = useState("");
  const [asOfDate, setAsOfDate] = useState("");
  const [error, setError] = useState(false);

  useEffect(() => { void api<Matter[]>("/matters").then(setMatters).catch(() => setError(true)); }, []);

  async function create(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setError(false);
    try {
      const matter = await api<Matter>("/matters", {
        method: "POST", body: JSON.stringify({
          name, court: court || null, as_of_date: asOfDate || null,
        }),
      });
      setMatters((previous) => [...(previous ?? []), matter]);
      setName(""); setCourt(""); setAsOfDate("");
    } catch { setError(true); } finally { setBusy(false); }
  }

  return <>
    <section className="hero compact">
      <p className="eyebrow">Litigation Argument Intelligence</p>
      <h1>Your legal work, in one place.</h1>
      <p>Start a matter to review a document, or research a question using the available sources.</p>
    </section>
    <div className="task-cards"><a href="#new-matter">Review a document</a><Link href="/research">Research a question</Link><a href="#my-matters">Continue a matter</a></div>
    <section className="matter-layout">
      <form id="new-matter" onSubmit={create}>
        <h2>Create a matter</h2>
        <label>Matter name<input required maxLength={200} value={name} onChange={(event) => setName(event.target.value)} placeholder="Smith v. Acme" /></label>
        <label>Court or forum<input value={court} onChange={(event) => setCourt(event.target.value)} placeholder="S.D.N.Y." /></label>
        <label>As-of date (optional)<input type="date" value={asOfDate} onChange={(event) => setAsOfDate(event.target.value)} /></label>
        <button disabled={busy}>{busy ? "Creating…" : "Create matter"}</button>
      </form>
      <div id="my-matters"><h2>Your matters</h2>
        {error && <p role="alert">Matter service is unavailable. Check the backend.</p>}
        {!error && !matters && <p role="status">Loading your matters…</p>}
        {!error && matters?.length === 0 && <p>No matters yet.</p>}
        <div className="matter-cards">{matters?.map((matter) =>
          <Link key={matter.matter_id} href={`/matters/${matter.matter_id}`}>
            <strong>{matter.name}</strong><span>{matter.court || matter.jurisdiction || "Forum unspecified"}</span>
          </Link>)}</div>
      </div>
    </section>
  </>;
}
