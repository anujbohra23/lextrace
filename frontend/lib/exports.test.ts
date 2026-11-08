import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { DOCX_TYPE, downloadDocx } from "./exports";
import { ApiError } from "./api";

let create: ReturnType<typeof vi.fn>;
let revoke: ReturnType<typeof vi.fn>;
beforeEach(() => {
  sessionStorage.setItem("lextrace.operatorToken", "operator-secret");
  create = vi.fn(() => "blob:word-download"); revoke = vi.fn();
  vi.stubGlobal("URL", { createObjectURL: create, revokeObjectURL: revoke });
});
afterEach(() => { sessionStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("authenticates binary downloads, honors safe attachment names and cleans up object URLs", async () => {
  const fetchMock = vi.fn(async () => ({ ok: true, headers: new Headers({ "content-type": DOCX_TYPE, "content-disposition": 'attachment; filename="Matter-deposition-r2.docx"' }), blob: async () => new Blob(["PK-document"], { type: DOCX_TYPE }) }) as Response); vi.stubGlobal("fetch", fetchMock);
  let filename = "";
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { filename = this.download; expect(this.href).toBe("blob:word-download"); });
  await downloadDocx("/matters/m/investigations/t/export.docx", "report.docx");
  expect(filename).toBe("Matter-deposition-r2.docx");
  const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).not.toContain("operator-secret");
  expect(new Headers(init.headers).get("authorization")).toBe("Bearer operator-secret");
  expect(init.cache).toBe("no-store");
  expect(revoke).toHaveBeenCalledWith("blob:word-download");
  expect(document.querySelector("a[download]")).toBeNull();
});

it("uses a local safe filename if a response offers an unsafe path", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(new Blob(["PK"], { type: DOCX_TYPE }), { headers: { "content-type": DOCX_TYPE, "content-disposition": 'attachment; filename="../../private.docx"' } })));
  let filename = ""; vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { filename = this.download; });
  await downloadDocx("/export.docx", "safe.docx"); expect(filename).toBe("safe.docx");
});

it("does not convert an authentication failure into a downloaded file", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("PRIVATE error", { status: 401 })));
  await expect(downloadDocx("/export.docx", "safe.docx")).rejects.toMatchObject({ status: 401 });
  expect(create).not.toHaveBeenCalled();
});

it("rejects wrong MIME and empty content without producing a blob URL", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("HTML error", { headers: { "content-type": "text/html" } })));
  await expect(downloadDocx("/export.docx", "safe.docx")).rejects.not.toBeInstanceOf(ApiError);
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, headers: new Headers({ "content-type": DOCX_TYPE }), blob: async () => new Blob([]) }) as Response));
  await expect(downloadDocx("/export.docx", "safe.docx")).rejects.toThrow("size");
  expect(create).not.toHaveBeenCalled();
});
