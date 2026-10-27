// The review queue: held media for admins to approve or reject.
// The brief requires three states that must never be confused: loading, failed, and
// genuinely empty. During the incident a failed request rendered as "Nothing to review"
// while 40 items waited, so "empty" here can only come from a successful response.
import { useCallback, useEffect, useState } from "react";
import type { Media } from "./api";

// A discriminated union: the component is always in exactly one of these states,
// and TypeScript forces every render path to handle each one.
type State =
  | { kind: "loading" }
  | { kind: "failed"; error: string; stale?: Media[] } // stale = last list that did load, if any
  | { kind: "loaded"; items: Media[] };

// Data access is passed in, not imported: tests can supply fake load/decide functions,
// and App decides which user the requests run as.
type Props = {
  load: () => Promise<Media[]>;
  decide: (id: string, action: "approve" | "reject") => Promise<unknown>;
  // INCIDENT 5 when true: a failed request is shown as an empty list ("Nothing to review").
  broken?: boolean;
  refreshMs?: number;
};

export function ReviewQueue({ load, decide, broken = false, refreshMs = 5000 }: Props) {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [actionError, setActionError] = useState<string | null>(null);

  // Fetch the queue and move to "loaded" or "failed". Called on mount, on a timer, after actions and by Retry.
  const refresh = useCallback(async () => {
    try {
      const items = await load();
      setState({ kind: "loaded", items });
    } catch (e) {
      if (broken) {
        // The incident's bug, kept only for the reproduction.
        setState({ kind: "loaded", items: [] });
        return;
      }
      // Keep whatever we showed before, but say clearly that it may be out of date.
      // (A refresh failing must not wipe a list a moderator is working through.)
      setState((prev) => ({
        kind: "failed",
        error: e instanceof Error ? e.message : String(e),
        stale: prev.kind === "loaded" ? prev.items : prev.kind === "failed" ? prev.stale : undefined,
      }));
    }
  }, [load, broken]);

  // Load now, then poll so items held during an outage appear (and clear) without a page reload.
  useEffect(() => {
    setState({ kind: "loading" });
    refresh();
    const t = setInterval(refresh, refreshMs);
    return () => clearInterval(t);
  }, [refresh, refreshMs]);

  // Approve or reject, then reload. A failed action (e.g. 403, 409) is shown, never swallowed.
  async function act(id: string, action: "approve" | "reject") {
    setActionError(null);
    try {
      await decide(id, action);
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
    refresh();
  }

  if (state.kind === "loading") return <p role="status">Loading review queue…</p>;

  const items = state.kind === "loaded" ? state.items : state.stale;
  return (
    <section>
      {state.kind === "failed" && (
        <div role="alert" className="error">
          Could not load the review queue ({state.error}). {items ? "Showing the last list we loaded; it may be out of date." : "Items may be waiting."}{" "}
          <button onClick={refresh}>Retry</button>
        </div>
      )}
      {actionError && <div role="alert" className="error">{actionError}</div>}
      {/* "Nothing to review" only for a successful, empty response. */}
      {state.kind === "loaded" && items!.length === 0 && <p>Nothing to review.</p>}
      {items && items.length > 0 && (
        <table>
          <thead>
            <tr><th>Media</th><th>Why held</th><th>Reasons</th><th></th></tr>
          </thead>
          <tbody>
            {items.map((m) => (
              <tr key={m.id}>
                <td><code>{m.id.slice(0, 8)}</code> {m.type}, {m.frame_count} frame(s), {m.source}</td>
                <td>{m.hold_kind === "provider_unavailable" ? "No decision yet (provider unavailable)" : "Needs a human"}</td>
                <td>{m.reasons.join("; ")}</td>
                <td>
                  {/* Approve is disabled for unscreened media: approving it would publish it unchecked
                      (the API also refuses with 409). Reject is always allowed. */}
                  <button
                    onClick={() => act(m.id, "approve")}
                    disabled={!m.screened}
                    title={m.screened ? "" : "Not screened yet; it will be re-screened automatically"}
                  >Approve</button>{" "}
                  <button onClick={() => act(m.id, "reject")}>Reject</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
