import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, USERS } from "./api";

const admin = USERS[0];

function mockFetch(status: number, body: string) {
  const fn = vi.fn(async () => new Response(body, { status }));
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => vi.unstubAllGlobals());

describe("api()", () => {
  it("sends the caller headers through the /api proxy", async () => {
    const fetch = mockFetch(200, "[]");
    await api(admin, "/review");
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/review");
    expect(init.headers).toMatchObject({ "X-User-Id": "admin_v", "X-Workspace-Id": "ws_verified" });
  });

  it("returns parsed JSON on success", async () => {
    mockFetch(200, '[{"id":"a"}]');
    expect(await api(admin, "/review")).toEqual([{ id: "a" }]);
  });

  it("throws on an error status with the API's detail", async () => {
    mockFetch(403, '{"detail":"only workspace admins can review media"}');
    await expect(api(admin, "/review")).rejects.toMatchObject({ status: 403, message: expect.stringMatching(/only workspace admins/) });
  });

  it("throws on an error status with a non-JSON body (proxy error page), never returns empty data", async () => {
    mockFetch(502, "<html>Bad Gateway</html>");
    await expect(api(admin, "/review")).rejects.toBeInstanceOf(ApiError);
  });

  it("surfaces refused prompt reasons from a 422", async () => {
    mockFetch(422, '{"reasons":["prompt refused: minor (\'16-year-old\')"]}');
    await expect(api(admin, "/media", { method: "POST" })).rejects.toThrow(/prompt refused: minor/);
  });

  it("propagates network failures", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    await expect(api(admin, "/review")).rejects.toThrow(/failed to fetch/i);
  });
});
