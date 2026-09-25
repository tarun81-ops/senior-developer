// One run, live: status, stage timeline and event log, all driven by the SSE
// stream (D19). The stream carries the events; the run state (stage
// timeline, status) is re-read from GET /api/runs/{id} whenever an event says
// it changed.
import { useEffect, useRef, useState } from "react";

import { CLOSING_KINDS } from "./api.js";
import ApprovalDialog from "./ApprovalDialog.jsx";
import DeployView from "./DeployView.jsx";
import FilesView from "./FilesView.jsx";
import TaskBoard from "./TaskBoard.jsx";

const TABS = [["timeline", "Timeline"], ["board", "Task board"], ["files", "Files"], ["deploy", "Deploy"]];

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

/** Events after which the run state (status, stages) is worth re-reading. */
const refreshes = (kind) => kind.startsWith("api.run_") || kind.startsWith("stage.") || kind.startsWith("pipeline.");

export default function RunView({ client, runId }) {
  const [run, setRun] = useState(null);
  const [events, setEvents] = useState([]);
  const [streamError, setStreamError] = useState(null);
  const [cancelling, setCancelling] = useState(false);
  // The dialog opens for each new wait (a gate can open again, e.g. the
  // execution gate on a fix-loop rerun). "Decide later" hides it until the next.
  const [dismissedWait, setDismissedWait] = useState(0);
  const [tab, setTab] = useState("timeline");
  const latest = useRef(0);

  useEffect(() => {
    const abort = new AbortController();
    // Only the newest response is applied, so two overlapping reads can never
    // leave an older state on screen. A failed read is retried while it is
    // still the newest: swallowing it would leave a stale state (e.g. no
    // approval dialog for a run that is waiting) until the next event.
    const refresh = () => {
      const ticket = ++latest.current;
      const current = () => ticket === latest.current && !abort.signal.aborted;
      const attempt = () => client.getRun(runId).then((state) => {
        if (current()) setRun(state);
      }).catch(() => {
        if (current()) setTimeout(attempt, 1000);
      });
      attempt();
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
  const waits = events.filter((e) => e.kind === "api.run_waiting_approval").length;
  const waiting = run?.status === "waiting_approval" && run.gate && !run.gate.decision;

  // A run waiting for a human says so in the window title (taskbar, alt-tab).
  useEffect(() => {
    const base = "Senior Developer Agents";
    document.title = waiting ? `Approval needed (${run.gate.gate}) · ${base}` : base;
    return () => { document.title = base; };
  }, [waiting, run?.gate?.gate]);
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
      {waiting && (
        <p className="notice">
          Waiting for your approval at the <strong>{run.gate.gate}</strong> gate.{" "}
          <button type="button" onClick={() => setDismissedWait(0)}>Review and decide</button>
        </p>
      )}
      {waiting && (
        <ApprovalDialog
          key={waits}
          client={client}
          run={run}
          open={dismissedWait !== waits}
          onClose={() => setDismissedWait(waits)}
        />
      )}
      {run?.error && <p className="error">{run.error}</p>}
      {streamError && <p className="error">Event stream: {streamError}</p>}
      {run && <StageTimeline stages={run.stage_states} />}
      <div className="tabs" role="tablist">
        {TABS.map(([id, label]) => (
          <button key={id} type="button" role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>
            {label}
          </button>
        ))}
      </div>
      {tab === "timeline" && <EventLog events={events} />}
      {tab === "board" && run && <TaskBoard run={run} />}
      {tab === "files" && <FilesView client={client} runId={runId} refreshKey={run?.status} />}
      {tab === "deploy" && <DeployView client={client} runId={runId} refreshKey={run?.status} />}
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
