"use client";

import { useRef, useState } from "react";
import { ApiError } from "@/lib/api";
import { downloadDocx } from "@/lib/exports";

export function exportFeedback(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.status) {
      case 401: return "Authentication required. Check the operator credential in this browser tab.";
      case 403: return "This operator cannot download material from this workspace.";
      case 404: return "The source investigation or approved revision is unavailable in this matter.";
      case 409: return "The source evidence or approval changed, or the report exceeds export limits. Refresh this workspace before downloading again.";
      case 422: return "The export request is invalid. Refresh and choose an approved revision.";
      case 503: return "Word export is unavailable or busy. Retry manually after checking server status.";
    }
  }
  return "The Word document could not be downloaded. No automatic retry was attempted.";
}

export function DocxDownload({ path, label, disabled, reason, filename }: {
  path: string; label: string; disabled: boolean; reason: string; filename: string;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const dispatching = useRef(false);
  async function download() {
    if (disabled || dispatching.current) return;
    dispatching.current = true; setBusy(true); setError("");
    try { await downloadDocx(path, filename); }
    catch (failure) { setError(exportFeedback(failure)); }
    finally { dispatching.current = false; setBusy(false); }
  }
  return <div className="docx-download">
    <button disabled={disabled || busy} onClick={() => void download()}>{busy ? `${label}…` : label}</button>
    {disabled && <p>{reason}</p>}
    {error && <p role="alert" className="notice">{error}</p>}
  </div>;
}
