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
import TerminalView from "./TerminalView.jsx";

const TABS = [
  ["timeline", "Timeline"],
  ["board", "Task board"],
  ["files", "Files"],
  ["terminal", "Terminal"],
  ["deploy", "Deploy"],
];

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
      {waiting && (
        <div className="notice notice-banner">
          <div>
            <span className="tag tag-accent">Waiting for approval</span>
            <p>Waiting for your approval at the <strong>{run.gate.gate}</strong> gate.</p>
          </div>
          <button type="button" className="approve" onClick={() => setDismissedWait(0)}>Review and decide</button>
        </div>
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
      <div className="run-head">
        <div>
          {run?.project && <span className="tag tag-neutral">Project · {run.project}</span>}
          <h1>{run ? run.request : "Loading run…"}</h1>
          <p className="muted">
            {run && `${startedLabel(run)} · `}run <code>{runId}</code>
          </p>
        </div>
        <div className="run-head-actions">
          <span className="status-pill" data-status={run?.status}>{statusText(run)}</span>
          {run && !finished && !closed && (
            <button type="button" className="cancel-run" onClick={cancel} disabled={cancelling || run.status === "cancelling"}>
              {cancelling || run.status === "cancelling" ? "Cancelling…" : "Cancel run"}
            </button>
          )}
        </div>
      </div>
      {run?.error && <p className="error">{run.error}</p>}
      {streamError && <p className="error">Event stream: {streamError}</p>}
      {run && (
        <>
          <span className="eyebrow">Pipeline</span>
          <StageTimeline stages={run.stage_states} agents={run.agents} />
        </>
      )}
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
      {tab === "terminal" && <TerminalView client={client} runId={runId} />}
      {tab === "deploy" && <DeployView client={client} runId={runId} refreshKey={run?.status} />}
    </section>
  );
}

function statusText(run) {
  if (!run) return "…";
  if (run.status === "queued" && run.queue_position) return `queued (#${run.queue_position})`;
  return run.status.replace("_", " ");
}

function startedLabel(run) {
  const at = run.started_at || run.created_at;
  return at ? `Started ${timeAgo(at)}` : "Not started yet";
}

function timeAgo(iso) {
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"} ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

function StageTimeline({ stages, agents }) {
  const byStage = new Map(agents.map((a) => [a.stage ?? a.agent, a]));
  return (
    <div className="pipeline" aria-label="Stages">
      {stages.map((stage, i) => {
        const output = byStage.get(stage.stage);
        return (
          <div className="stage-row" key={stage.stage}>
            <p className="stage-n">{String(i + 1).padStart(2, "0")}</p>
            <h3 className="stage-name">{stage.stage}</h3>
            <p className="stage-model muted">{output?.target || ""}</p>
            <div className="stage-end">
              <span className="stage-meta muted">{stageMeta(stage.status, output)}</span>
              <span className="tag" data-status={stage.status}>{stage.status}</span>
            </div>
            {stage.error && <p className="error stage-error">{stage.error}</p>}
          </div>
        );
      })}
    </div>
  );
}

function stageMeta(status, output) {
  if (status === "running") return "in progress";
  if (status === "pending") return "not started";
  if (status === "skipped") return "skipped";
  if (!output) return "";
  const bits = [];
  if (output.duration_ms != null) bits.push(`${(output.duration_ms / 1000).toFixed(1)}s`);
  const tokens = Object.values(output.tokens).reduce((a, b) => a + b, 0);
  if (tokens) bits.push(`${tokens} tok`);
  return bits.join(" · ");
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
