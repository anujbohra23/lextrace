import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DocxDownload } from "./DocxDownload";
import { DOCX_TYPE } from "@/lib/exports";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
const props = { path: "/matters/m/investigations/t/export.docx", label: "Download investigation report", filename: "report.docx", reason: "A current signed investigation snapshot is required." };

it("shows why export is disabled without making a request", () => {
  const fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock);
  render(<DocxDownload {...props} disabled />);
  expect(screen.getByRole("button", { name: props.label })).toBeDisabled();
  expect(screen.getByText(props.reason)).toBeInTheDocument(); expect(fetchMock).not.toHaveBeenCalled();
});

it("prevents duplicate requests during a download", async () => {
  let finish!: (response: Response) => void;
  const fetchMock = vi.fn(() => new Promise<Response>((resolve) => { finish = resolve; })); vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:docx"), revokeObjectURL: vi.fn() });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  render(<DocxDownload {...props} disabled={false} />);
  const button = screen.getByRole("button", { name: props.label });
  fireEvent.click(button); fireEvent.click(button);
  expect(fetchMock).toHaveBeenCalledTimes(1); expect(button).toBeDisabled();
  finish(new Response(new Blob(["PK"], { type: DOCX_TYPE }), { headers: { "content-type": DOCX_TYPE } }));
  await waitFor(() => expect(button).toBeEnabled());
});

it.each([401, 403, 404, 409, 422, 503])("reports HTTP %i safely with no automatic repeated export", async (status) => {
  const fetchMock = vi.fn(async () => new Response("SECRET /private/path", { status })); vi.stubGlobal("fetch", fetchMock);
  render(<DocxDownload {...props} disabled={false} />); fireEvent.click(screen.getByRole("button", { name: props.label }));
  const error = await screen.findByRole("alert"); expect(error.textContent).not.toContain("SECRET"); expect(error.textContent).not.toContain("/private/path");
  expect(fetchMock).toHaveBeenCalledTimes(1);
});
