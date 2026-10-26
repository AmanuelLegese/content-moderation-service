import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Media } from "./api";
import { ReviewQueue } from "./ReviewQueue";

afterEach(cleanup);

const noop = async () => undefined;
const item: Media = {
  id: "11111111-2222", status: "held", reasons: ["possible minor 0.60 at frame 0"], type: "image",
  source: "upload", frame_count: 1, hold_kind: "review", screened: true, created_at: "",
};

function deferred<T>() {
  let resolve!: (v: T) => void, reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => ((resolve = res), (reject = rej)));
  return { promise, resolve, reject };
}

describe("ReviewQueue tells loading, failed and empty apart", () => {
  it("shows loading while the request is in flight", () => {
    render(<ReviewQueue load={() => new Promise(() => {})} decide={noop} />);
    expect(screen.getByRole("status").textContent).toMatch(/loading/i);
    expect(screen.queryByText(/nothing to review/i)).toBeNull();
  });

  it("shows a failed request as an error, never as 'Nothing to review'", async () => {
    const d = deferred<Media[]>();
    render(<ReviewQueue load={() => d.promise} decide={noop} />);
    await act(async () => d.reject(new Error("503: upstream unavailable")));
    expect(screen.getByRole("alert").textContent).toMatch(/could not load the review queue/i);
    expect(screen.queryByText(/nothing to review/i)).toBeNull();
  });

  it("shows 'Nothing to review' only for a successful empty response", async () => {
    render(<ReviewQueue load={async () => []} decide={noop} />);
    expect(await screen.findByText(/nothing to review/i)).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps the last loaded items and flags them as stale when a refresh fails", async () => {
    let fail = false;
    render(<ReviewQueue load={async () => { if (fail) throw new Error("timeout"); return [item]; }} decide={noop} refreshMs={20} />);
    expect(await screen.findByText(/possible minor/)).toBeTruthy();
    fail = true;
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText(/possible minor/)).toBeTruthy();
  });

  it("labels outage-held media and disables Approve until it has been screened", async () => {
    const unscreened = { ...item, id: "33333333-4444", hold_kind: "provider_unavailable" as const, screened: false,
      reasons: ["no moderation decision yet (timeout)"] };
    render(<ReviewQueue load={async () => [item, unscreened]} decide={noop} />);
    expect(await screen.findByText(/no decision yet \(provider unavailable\)/i)).toBeTruthy();
    const approves = screen.getAllByRole("button", { name: "Approve" }) as HTMLButtonElement[];
    expect(approves.map((b) => b.disabled)).toEqual([false, true]);
    // Reject stays available: a human may always take something down
    expect((screen.getAllByRole("button", { name: "Reject" }) as HTMLButtonElement[]).every((b) => !b.disabled)).toBe(true);
  });

  it("approve calls the API and reloads the queue", async () => {
    const decide = vi.fn(async () => undefined);
    let items = [item];
    const load = vi.fn(async () => items);
    render(<ReviewQueue load={load} decide={decide} refreshMs={60_000} />);
    await screen.findByText(/possible minor/);
    items = [];
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Approve" })));
    expect(decide).toHaveBeenCalledWith(item.id, "approve");
    expect(await screen.findByText(/nothing to review/i)).toBeTruthy();
  });

  it("shows a failed approve (e.g. 403 for a member) instead of swallowing it", async () => {
    const decide = vi.fn(async () => { throw new Error("403: only workspace admins can review media"); });
    render(<ReviewQueue load={async () => [item]} decide={decide} refreshMs={60_000} />);
    await screen.findByText(/possible minor/);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Reject" })));
    expect((await screen.findByRole("alert")).textContent).toMatch(/403/);
    expect(screen.getByText(/possible minor/)).toBeTruthy();
  });

  it("INCIDENT reproduction: the broken handler hides the failure as an empty list", async () => {
    render(<ReviewQueue load={async () => { throw new Error("503"); }} decide={noop} broken />);
    // This is the bug: moderators saw "Nothing to review" while items were waiting.
    expect(await screen.findByText(/nothing to review/i)).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
