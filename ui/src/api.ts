export type Media = {
  id: string;
  status: "pending" | "published" | "held" | "blocked";
  reasons: string[];
  type: string;
  source: string;
  frame_count: number;
  hold_kind: "review" | "provider_unavailable" | null;
  screened: boolean;
  created_at: string;
};

export type User = { id: string; workspace: string; role: "admin" | "member" };

export const USERS: User[] = [
  { id: "admin_v", workspace: "ws_verified", role: "admin" },
  { id: "member_v", workspace: "ws_verified", role: "member" },
  { id: "admin_u", workspace: "ws_unverified", role: "admin" },
  { id: "member_u", workspace: "ws_unverified", role: "member" },
];

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

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
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    // non-JSON (e.g. a proxy error page) is still an error, never "no data"
  }
  if (!res.ok) {
    const detail = (body as { detail?: unknown; reasons?: unknown })?.detail ?? (body as { reasons?: unknown })?.reasons;
    throw new ApiError(res.status, `${res.status}: ${detail ? JSON.stringify(detail) : text.slice(0, 120) || res.statusText}`);
  }
  return body as T;
}
