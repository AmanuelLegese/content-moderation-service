import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn(async (url: string) => {
    if (url.startsWith("/api/review")) return new Response("[]", { status: 200 });
    if (url.startsWith("/api/media")) return new Response("[]", { status: 200 });
    return new Response("not found", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const urls = () => fetchMock.mock.calls.map((c) => c[0] as string);

describe("App", () => {
  it("an admin sees the review queue", async () => {
    render(<App />);
    expect(await screen.findByText(/nothing to review/i)).toBeTruthy();
    expect(urls()).toContain("/api/review");
  });

  it("a member does not see the review queue and never requests it", async () => {
    render(<App />);
    await screen.findByText(/nothing to review/i);
    fetchMock.mockClear();
    await act(async () => fireEvent.change(screen.getByRole("combobox"), { target: { value: "member_u" } }));
    expect(screen.getByText(/only workspace admins can review/i)).toBeTruthy();
    expect(urls().some((u) => u.startsWith("/api/review"))).toBe(false);
    const headers = fetchMock.mock.calls.map((c) => (c[1] as RequestInit).headers as Record<string, string>);
    expect(headers.every((h) => h["X-Workspace-Id"] === "ws_unverified")).toBe(true);
  });

  it("shows a failed media list as an error, not as 'No media yet'", async () => {
    fetchMock.mockImplementation(async (url: string) =>
      url.startsWith("/api/media") ? new Response("down", { status: 503 }) : new Response("[]", { status: 200 }));
    render(<App />);
    expect(await screen.findByText(/could not load media/i)).toBeTruthy();
    expect(screen.queryByText(/no media yet/i)).toBeNull();
  });
});
