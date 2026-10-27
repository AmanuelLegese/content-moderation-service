// The single page: pick a user, see the review queue (admins only), recent media, and an upload box.
import { useCallback, useEffect, useState } from "react";
import { api, USERS, type Media, type User } from "./api";
import { ReviewQueue } from "./ReviewQueue";

// Baked in at build time from HANDLER (docker-compose passes it as a build arg), so the UI
// reproduces its part of the incident exactly when the backend does.
const BROKEN = import.meta.env.VITE_HANDLER === "broken";
// Pre-filled upload: the example document from the brief.
const EXAMPLE = JSON.stringify(
  { type: "video", source: "generation", prompt: "two adults on a beach at sunset", frames: [{ explicit: 0.02 }, { explicit: 0.95, self_harm: 0.1 }] },
  null,
  2,
);

export function App() {
  const [user, setUser] = useState<User>(USERS[0]);
  // null = not loaded yet; [] = loaded and empty. Kept apart for the same reason as the review queue.
  const [media, setMedia] = useState<Media[] | null>(null);
  const [mediaError, setMediaError] = useState<string | null>(null);
  const [draft, setDraft] = useState(EXAMPLE);
  const [uploadResult, setUploadResult] = useState<string | null>(null);

  // useCallback keeps these functions stable until the user changes, so ReviewQueue's
  // effect doesn't restart its polling on every render.
  const loadReview = useCallback(() => api<Media[]>(user, "/review"), [user]);
  const decide = useCallback(
    (id: string, action: "approve" | "reject") => api(user, `/review/${id}/${action}`, { method: "POST" }),
    [user],
  );

  // Poll the workspace's recent media every 3s so status changes (pending -> held -> published) show up.
  useEffect(() => {
    // `live` ignores responses that arrive after the user switched: they belong to the old workspace.
    let live = true;
    const load = () =>
      api<Media[]>(user, "/media?limit=20")
        .then((m) => live && (setMedia(m), setMediaError(null)))
        .catch((e) => live && setMediaError(String(e.message ?? e)));
    setMedia(null);
    load();
    const t = setInterval(load, 3000);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, [user]);

  // Submit the textarea as-is. The server validates it; a refused prompt's reasons are shown.
  async function upload() {
    try {
      const r = await api<{ id: string }>(user, "/media", { method: "POST", body: draft });
      setUploadResult(`Queued ${r.id}`);
    } catch (e) {
      setUploadResult(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <main>
      <header>
        <h1>Review queue</h1>
        <label>
          Acting as{" "}
          <select value={user.id} onChange={(e) => setUser(USERS.find((u) => u.id === e.target.value)!)}>
            {USERS.map((u) => (
              <option key={u.id} value={u.id}>{u.id} ({u.role}, {u.workspace})</option>
            ))}
          </select>
        </label>
        {BROKEN && <strong className="error"> BROKEN handler (incident reproduction)</strong>}
      </header>

      {/* Members never even request /review (the API would answer 403; rule 6).
          key={user.id} remounts the queue on user switch so it starts again from "loading". */}
      {user.role === "admin" ? (
        <ReviewQueue key={user.id} load={loadReview} decide={decide} broken={BROKEN} />
      ) : (
        <p>Only workspace admins can review held media.</p>
      )}

      <h2>Recent media in {user.workspace}</h2>
      {mediaError && <div role="alert" className="error">Could not load media: {mediaError}</div>}
      {media === null && !mediaError && <p>Loading…</p>}
      {media && media.length === 0 && <p>No media yet.</p>}
      {media && media.length > 0 && (
        <table>
          <thead><tr><th>Media</th><th>Status</th><th>Reasons</th></tr></thead>
          <tbody>
            {media.map((m) => (
              <tr key={m.id}>
                <td><code>{m.id.slice(0, 8)}</code> {m.type} ({m.frame_count})</td>
                <td className={`status ${m.status}`}>{m.status}</td>
                <td>{m.reasons.join("; ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2>Upload or generate</h2>
      <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={8} />
      <div><button onClick={upload}>Submit</button> {uploadResult}</div>
    </main>
  );
}
