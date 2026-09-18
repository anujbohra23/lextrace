import { act, cleanup, render, screen, fireEvent } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ResearchRun } from "./ResearchRun";
vi.mock("next/navigation", () => ({useRouter: () => ({push: vi.fn()})}));
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });
const response = (value: unknown) => ({ok:true, json:async () => value});
test("queued research stays queued beyond two minutes and survives remount", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("fetch", vi.fn(async () => response({run_id:"a",status:"queued",created_at:new Date().toISOString(),result:null})));
  const view = render(<ResearchRun runId="a" />);
  await act(async () => { await vi.advanceTimersByTimeAsync(130000); });
  expect(screen.getByText("Queued")).toBeInTheDocument();
  expect(screen.queryByText(/could not finish/)).not.toBeInTheDocument();
  view.unmount(); render(<ResearchRun runId="a" />);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getByText("Queued")).toBeInTheDocument();
  expect(fetch).not.toHaveBeenCalledWith("/research", expect.anything());
});
test("network failure offers recovery without submitting another job", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValueOnce(new Error("sensitive dependency error")).mockResolvedValue(response({run_id:"a",status:"running",created_at:new Date().toISOString(),progress:{stage:"retrieval"}})));
  render(<ResearchRun runId="a" />);
  expect(await screen.findByRole("alert")).not.toHaveTextContent("sensitive dependency error");
  fireEvent.click(screen.getByRole("button",{name:"Check again"}));
  expect(await screen.findByText("Retrieval")).toBeInTheDocument();
});
test("interrupted run provides deliberate retry of the same saved run", async () => {
  const fetchMock = vi.fn(async () => response({run_id:"a",status:"failed",created_at:new Date().toISOString(),can_resume:true,error_code:"interrupted"}));
  vi.stubGlobal("fetch",fetchMock); render(<ResearchRun runId="a" />);
  fireEvent.click(await screen.findByRole("button",{name:"Retry saved research"}));
  expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/research/a/resume"),expect.objectContaining({method:"POST"}));
});
