import { API_URL, ApiError, operatorHeaders } from "./api";

export const DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const MAX_DOCX_BYTES = 8 * 1024 * 1024;

/** Authenticated binary fetch; credentials never appear in links or filenames. */
export async function downloadDocx(path: string, fallbackName: string): Promise<void> {
  const response = await fetch(API_URL + path, { headers: operatorHeaders(), cache: "no-store" });
  if (!response.ok) throw new ApiError(response.status);
  if (response.headers.get("content-type")?.split(";")[0] !== DOCX_TYPE) throw new Error("Invalid Word response.");
  const blob = await response.blob();
  if (blob.size === 0 || blob.size > MAX_DOCX_BYTES) throw new Error("Invalid Word response size.");
  const disposition = response.headers.get("content-disposition") ?? "";
  const candidate = /filename="([A-Za-z0-9][A-Za-z0-9.-]{0,119}\.docx)"/.exec(disposition)?.[1];
  const filename = candidate && !candidate.includes("..") ? candidate : fallbackName;
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  try {
    link.href = url; link.download = filename;
    document.body.appendChild(link); link.click();
  } finally {
    link.remove(); URL.revokeObjectURL(url);
  }
}
