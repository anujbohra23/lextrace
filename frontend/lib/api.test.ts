import { afterEach, describe, expect, it, vi } from "vitest";
import { api, downloadMatrix, operatorHeaders } from "./api";
import { uploadDocument } from "./matter";

afterEach(() => {
  sessionStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("pilot bearer credential", () => {
  it("does not invent a credential", () => {
    expect(operatorHeaders()).toEqual({});
  });

  it("sends the tab credential on JSON requests and uploads, never in URLs", async () => {
    sessionStorage.setItem("lextrace.operatorToken", "synthetic-test-token");
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);
    await api("/matters");
    await uploadDocument("matter", new File(["sample"], "sample.txt"));
    for (const [url, init] of fetchMock.mock.calls) {
      expect(url).not.toContain("synthetic-test-token");
      expect(init.headers.Authorization).toBe("Bearer synthetic-test-token");
    }
    expect(fetchMock.mock.calls[1][1].headers["content-type"]).toBeUndefined();
  });

  it("authenticates CSV downloads and revokes the object URL", async () => {
    sessionStorage.setItem("lextrace.operatorToken", "synthetic-test-token");
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, blob: async () => new Blob(["csv"]) });
    vi.stubGlobal("fetch", fetchMock);
    const createObjectURL = vi.fn().mockReturnValue("blob:local-download");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    await downloadMatrix("matter");
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe("Bearer synthetic-test-token");
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:local-download");
  });
});

it("preserves HTTP status for investigation feedback without echoing server error data", async () => {
  const { ApiError } = await import("./api");
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 409 }));
  try { await api("/matters/matter/investigations/thread"); }
  catch (failure) {
    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as InstanceType<typeof ApiError>).status).toBe(409);
    expect((failure as Error).message).toBe("LexTrace request failed.");
    return;
  }
  throw new Error("Expected the request to fail");
});
