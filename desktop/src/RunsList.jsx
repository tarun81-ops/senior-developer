// Run history (B6, D32): every run the API knows, newest first, including
// runs from earlier launches. Re-read when a run is created, every 2 s while
// any listed run is active, and every 10 s otherwise, so a run started
// anywhere else (another client, the API directly) still appears (D34).
import { useEffect, useState } from "react";

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
const POLL_ACTIVE_MS = 2000;
const POLL_IDLE_MS = 10000;

export default function RunsList({ client, selected, onSelect, refreshKey }) {
  const [runs, setRuns] = useState(null);
  const [error, setError] = useState(null);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    let live = true;
    let timer = null;
    const load = () => client.listRuns()
      .then((body) => {
        if (!live) return;
        setRuns(body.runs);
        setError(null);
        const active = body.runs.some((r) => !TERMINAL.has(r.status));
        timer = setTimeout(load, active ? POLL_ACTIVE_MS : POLL_IDLE_MS);
      })
      .catch((err) => {
        if (!live) return;
        setError(err.message);
        timer = setTimeout(load, POLL_IDLE_MS); // keep trying; the error clears on success
      });
    load();
    return () => { live = false; clearTimeout(timer); };
  }, [client, refreshKey, retry]);

  return (
    <section className="card runs-list" aria-label="Runs">
      <h2>Runs</h2>
      {error && (
        <p className="error">
          Could not load runs: {error}{" "}
          <button type="button" onClick={() => setRetry((n) => n + 1)}>Try again</button>
        </p>
      )}
      {!runs && !error && <p className="muted">Loading…</p>}
      {runs?.length === 0 && <p className="muted">No runs yet. Start one above.</p>}
      {runs?.length > 0 && (
        <ul>
          {runs.map((run) => (
            <li key={run.run_id}>
              <button
                type="button"
                className="run-item"
                data-run={run.run_id}
                aria-current={run.run_id === selected ? "true" : undefined}
                onClick={() => onSelect(run.run_id)}
              >
                <span className="run-item-request">{run.request}</span>
                <span className="run-item-meta">
                  <span className="status-pill" data-status={run.status}>{run.status.replace("_", " ")}</span>
                  <span className="muted">{formatWhen(run.created_at)}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function formatWhen(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const sameDay = date.toDateString() === new Date().toDateString();
  return sameDay ? date.toLocaleTimeString() : date.toLocaleString();
}
