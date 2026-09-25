import { useState } from "react";

import DevMenu from "./DevMenu.jsx";
import { GATES, buildRunRequest, freshForm } from "./runRequest.js";

const GATE_HELP = {
  plan: "after the planner",
  architecture: "after the architect",
  execution: "before any command runs",
};

export default function NewRunForm({ client, onCreated }) {
  const [form, setForm] = useState(freshForm);
  const [override, setOverride] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const run = await client.createRun(buildRunRequest(form, override));
      setForm(freshForm()); // gates back to defaults: execution on, every time
      onCreated(run.run_id);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const setGate = (gate) => (e) => setForm({ ...form, gates: { ...form.gates, [gate]: e.target.checked } });

  return (
    <form className="card" onSubmit={submit} aria-label="New run">
      <h2>New run</h2>
      <label className="field">
        What should the agents build?
        <textarea
          name="request"
          rows={3}
          value={form.request}
          onChange={(e) => setForm({ ...form, request: e.target.value })}
          required
        />
      </label>
      <label className="field">
        Project folder name (optional)
        <input name="project" value={form.project} onChange={(e) => setForm({ ...form, project: e.target.value })} />
      </label>
      <fieldset>
        <legend>Stop for my approval</legend>
        {GATES.map((gate) => (
          <label key={gate} className="check">
            <input type="checkbox" name={`gate-${gate}`} checked={form.gates[gate]} onChange={setGate(gate)} />{" "}
            {gate} <span className="muted">({GATE_HELP[gate]})</span>
          </label>
        ))}
      </fieldset>
      {import.meta.env.DEV && <DevMenu override={override} onChange={setOverride} />}
      {error && <p className="error">{error}</p>}
      <button type="submit" disabled={busy}>{busy ? "Starting…" : "Start run"}</button>
    </form>
  );
}
