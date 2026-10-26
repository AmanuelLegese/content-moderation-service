import { useCallback, useEffect, useState } from "react";
import type { Media } from "./api";

type State =
  | { kind: "loading" }
  | { kind: "failed"; error: string; stale?: Media[] }
  | { kind: "loaded"; items: Media[] };

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

  const refresh = useCallback(async () => {
    try {
      const items = await load();
      setState({ kind: "loaded", items });
    } catch (e) {
      if (broken) {
        setState({ kind: "loaded", items: [] });
        return;
      }
      // Keep whatever we showed before, but say clearly that it may be out of date.
      setState((prev) => ({
        kind: "failed",
        error: e instanceof Error ? e.message : String(e),
        stale: prev.kind === "loaded" ? prev.items : prev.kind === "failed" ? prev.stale : undefined,
      }));
    }
  }, [load, broken]);

  useEffect(() => {
    setState({ kind: "loading" });
    refresh();
    const t = setInterval(refresh, refreshMs);
    return () => clearInterval(t);
  }, [refresh, refreshMs]);

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
