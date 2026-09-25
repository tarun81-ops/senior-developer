// The approval dialog (B3, D21, D29). Opens while a run waits at a gate:
//   plan / architecture -> the stage's full output, as sanitized markdown;
//   execution           -> the exact command, working folder and timeout,
//                          verbatim and never rendered as markdown.
// Approve and Reject go through the token-guarded API. showModal() focuses
// the first focusable element, which is the note field (the output above it
// holds no links or buttons), so Enter can never approve by accident.
import { useEffect, useRef, useState } from "react";

import SafeMarkdown from "./SafeMarkdown.js";

const TITLES = {
  plan: "Approve the plan?",
  architecture: "Approve the architecture?",
  execution: "Run this command?",
};

export default function ApprovalDialog({ client, run, open, onClose }) {
  const dialog = useRef(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(null); // "approve" | "reject" | null
  const [error, setError] = useState(null);
  const gate = run.gate;

  useEffect(() => {
    const el = dialog.current;
    if (open && !el.open) el.showModal();
    if (!open && el.open) el.close();
  }, [open]);

  async function decide(action) {
    setBusy(action);
    setError(null);
    try {
      await client[action](run.run_id, note.trim() || undefined);
      setNote("");
      onClose();
    } catch (err) {
      // 409: the run is no longer waiting (cancelled or already decided)
      setError(err.status === 409 ? "This run is no longer waiting for a decision." : err.message);
    } finally {
      setBusy(null);
    }
  }

  return (
    // Escape closes the dialog without deciding; the run keeps waiting and
    // the run view offers to reopen it.
    <dialog ref={dialog} className="approval" aria-labelledby="approval-title" onClose={onClose}>
      <h2 id="approval-title">{TITLES[gate.gate] ?? `Approve ${gate.gate}?`}</h2>
      {gate.gate === "execution" ? <CommandDetails payload={gate.payload} /> : <StageOutput run={run} gate={gate} />}
      <label className="field">
        Note (optional; shown in the run's history, and as the reason if you reject)
        <textarea name="approval-note" rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      {error && <p className="error">{error}</p>}
      <div className="actions">
        <button type="button" onClick={onClose} disabled={busy !== null}>Decide later</button>
        <button type="button" className="reject" onClick={() => decide("reject")} disabled={busy !== null}>
          {busy === "reject" ? "Rejecting…" : "Reject"}
        </button>
        <button type="button" className="approve" onClick={() => decide("approve")} disabled={busy !== null}>
          {busy === "approve" ? "Approving…" : gate.gate === "execution" ? "Approve and run" : "Approve"}
        </button>
      </div>
    </dialog>
  );
}

function CommandDetails({ payload }) {
  return (
    <div className="command-details">
      <p>The agents want to run this command on your computer:</p>
      <dl>
        <dt>Command</dt>
        <dd><pre className="verbatim" data-field="command">{payload.command}</pre></dd>
        <dt>Working folder</dt>
        <dd><pre className="verbatim" data-field="cwd">{payload.cwd}</pre></dd>
        <dt>Time limit</dt>
        <dd data-field="timeout">{payload.timeout_seconds} seconds (then its whole process tree is killed)</dd>
      </dl>
    </div>
  );
}

function StageOutput({ run, gate }) {
  // The gate's payload is cut at 4,000 characters; the run state holds the
  // stage's full output, which is what is actually being approved.
  const stage = run.stage_states.find((s) => s.stage === gate.payload.stage);
  const text = stage?.output ?? gate.payload.output ?? "";
  return (
    <div className="stage-output">
      <p className="muted">Output of the <strong>{gate.payload.stage}</strong> stage:</p>
      <SafeMarkdown text={text} />
    </div>
  );
}
