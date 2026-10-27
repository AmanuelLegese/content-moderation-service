// Thin client for the moderation API, shared by every component.
// Why one helper: the incident's UI bug was a failed request turning into "no data".
// Funnelling every request through api() means there is exactly one place where
// errors are detected, and it always throws instead of returning something empty.

// Shape of a media item as returned by the API (see _public() in server/app/api.py).
export type Media = {
  id: string;
  status: "pending" | "published" | "held" | "blocked";
  reasons: string[];
  type: string;
  source: string;
  frame_count: number;
  // Why it is held: a human must decide ("review"), or there is no decision yet ("provider_unavailable").
  hold_kind: "review" | "provider_unavailable" | null;
  // True only after a complete provider result was applied; Approve is disabled until then.
  screened: boolean;
  created_at: string;
};

export type User = { id: string; workspace: string; role: "admin" | "member" };

// The seeded users (server/app/db.py). There is no login: the user picker sends these as headers.
export const USERS: User[] = [
  { id: "admin_v", workspace: "ws_verified", role: "admin" },
  { id: "member_v", workspace: "ws_verified", role: "member" },
  { id: "admin_u", workspace: "ws_unverified", role: "admin" },
  { id: "member_u", workspace: "ws_unverified", role: "member" },
];

// An HTTP error with its status code, so callers can tell 403 from 503 if they need to.
export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

// Call the API as `user`. Resolves with the parsed JSON on 2xx; throws ApiError otherwise.
// Requests go to /api/..., which nginx (and the Vite dev server) proxy to the API on port 8000,
// so the browser sees one origin and no CORS setup is needed.
export async function api<T>(user: User, path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-User-Id": user.id,
      "X-Workspace-Id": user.workspace,
      ...(init.headers ?? {}),
    },
  });
  // Read as text first: an error response may not be JSON at all.
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    // non-JSON (e.g. a proxy error page) is still an error, never "no data"
  }
  if (!res.ok) {
    // FastAPI errors carry `detail`; a refused generation prompt (422) carries `reasons`.
    const detail = (body as { detail?: unknown; reasons?: unknown })?.detail ?? (body as { reasons?: unknown })?.reasons;
    throw new ApiError(res.status, `${res.status}: ${detail ? JSON.stringify(detail) : text.slice(0, 120) || res.statusText}`);
  }
  return body as T;
}
