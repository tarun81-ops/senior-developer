// One run, live: status, stage timeline and event log, all driven by the SSE
// stream (D19). The stream carries the events; the run state (stage
// timeline, status) is re-read from GET /api/runs/{id} whenever an event says
// it changed.
import { useEffect, useRef, useState } from "react";

import { CLOSING_KINDS } from "./api.js";

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

/** Events after which the run state (status, stages) is worth re-reading. */
const refreshes = (kind) => kind.startsWith("api.run_") || kind.startsWith("stage.") || kind.startsWith("pipeline.");

export default function RunView({ client, runId }) {
  const [run, setRun] = useState(null);
  const [events, setEvents] = useState([]);
  const [streamError, setStreamError] = useState(null);
  const [cancelling, setCancelling] = useState(false);
  const latest = useRef(0);

  useEffect(() => {
    const abort = new AbortController();
    // Only the newest response is applied, so two overlapping reads can never
    // leave an older state on screen.
    const refresh = () => {
      const ticket = ++latest.current;
      client.getRun(runId).then((state) => {
        if (ticket === latest.current && !abort.signal.aborted) setRun(state);
      }).catch(() => {});
    };
    refresh();
    client
      .followRun(runId, (event) => {
        setEvents((prev) => [...prev, event]);
        if (refreshes(event.kind)) refresh();
      }, { signal: abort.signal })
      .then(refresh)
      .catch((err) => { if (!abort.signal.aborted) setStreamError(err.message); });
    return () => abort.abort();
  }, [client, runId]);

  const finished = run ? TERMINAL.has(run.status) : false;
  const closed = events.some((e) => CLOSING_KINDS.has(e.kind));

  async function cancel() {
    setCancelling(true);
    try { await client.cancel(runId); } catch (err) { setStreamError(err.message); }
  }

  return (
    <section className="card run" aria-label="Run">
      <div className="run-head">
        <h2>{run ? run.request : "Loading run…"}</h2>
        <span className="status-pill" data-status={run?.status}>{statusText(run)}</span>
        {run && !finished && !closed && (
          <button type="button" onClick={cancel} disabled={cancelling || run.status === "cancelling"}>
            {cancelling || run.status === "cancelling" ? "Cancelling…" : "Cancel run"}
          </button>
        )}
      </div>
      <p className="muted">
        Run <code>{runId}</code>{run?.project && <> · project <code>{run.project}</code></>}
      </p>
      {run?.status === "waiting_approval" && run.gate && (
        <p className="notice">
          Waiting for your approval at the <strong>{run.gate.gate}</strong> gate. Approval dialogs
          arrive in the next UI phase; until then you can cancel the run.
        </p>
      )}
      {run?.error && <p className="error">{run.error}</p>}
      {streamError && <p className="error">Event stream: {streamError}</p>}
      {run && <StageTimeline stages={run.stage_states} />}
      <EventLog events={events} />
    </section>
  );
}

function statusText(run) {
  if (!run) return "…";
  if (run.status === "queued" && run.queue_position) return `queued (#${run.queue_position})`;
  return run.status.replace("_", " ");
}

function StageTimeline({ stages }) {
  return (
    <ol className="timeline" aria-label="Stages">
      {stages.map((stage) => (
        <li key={stage.stage} data-status={stage.status}>
          <span className="stage-name">{stage.stage}</span>
          <span className="stage-status">{stage.status}</span>
          {stage.error && <span className="error"> {stage.error}</span>}
        </li>
      ))}
    </ol>
  );
}

function EventLog({ events }) {
  const bottom = useRef(null);
  useEffect(() => { bottom.current?.scrollIntoView({ block: "nearest" }); }, [events.length]);
  return (
    <div className="events" aria-label="Events">
      <table>
        <tbody>
          {events.map((e) => (
            <tr key={e.seq} data-kind={e.kind}>
              <td className="muted">{formatTime(e.ts)}</td>
              <td><code>{e.kind}</code></td>
              <td>{e.agent ?? ""}</td>
              <td>{e.message}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div ref={bottom} />
    </div>
  );
}

function formatTime(ts) {
  const date = new Date(ts);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString();
}
