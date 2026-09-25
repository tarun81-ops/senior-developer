// The app shell: connect over the bridge (D26), check health, then two
// screens: Runs (new-run form + the selected run, live) and Settings.
import { useEffect, useState } from "react";

import { createClient } from "./api.js";
import NewRunForm from "./NewRunForm.jsx";
import RunsList from "./RunsList.jsx";
import RunView from "./RunView.jsx";
import SettingsView from "./SettingsView.jsx";

async function connect() {
  if (!window.sda) throw new Error("Open this app through the desktop shell (npm start or npm run dev).");
  const client = createClient(await window.sda.connection());
  return { client, health: await client.health() };
}

export default function App() {
  const [status, setStatus] = useState({ state: "connecting", text: "Connecting to the local API…" });
  const [client, setClient] = useState(null);
  const [screen, setScreen] = useState("runs");
  const [runId, setRunId] = useState(null);
  const [created, setCreated] = useState(0); // bumps the runs list after a new run
  // Settings warnings (e.g. an ignored overrides file) flag the nav from
  // startup, not only once the Settings screen is opened (D31).
  const [warnings, setWarnings] = useState([]);

  useEffect(() => {
    let live = true;
    connect()
      .then(({ client: api, health }) => {
        if (!live) return;
        setClient(api);
        setStatus({ state: "ok", text: `Connected: API ${health.version} on port ${health.api_port}` });
        api.getSettings().then((body) => live && setWarnings(body.warnings)).catch(() => {});
      })
      .catch((err) => live && setStatus({ state: "error", text: String(err.message || err) }));
    return () => { live = false; };
  }, []);

  return (
    <main className="shell">
      <header>
        <h1>Senior Developer Agents</h1>
        {client && (
          <nav className="screens">
            <button type="button" aria-current={screen === "runs" ? "page" : undefined} onClick={() => setScreen("runs")}>
              Runs
            </button>
            <button type="button" aria-current={screen === "settings" ? "page" : undefined} onClick={() => setScreen("settings")}>
              Settings{warnings.length > 0 && <span className="badge" title="Your saved settings were ignored">!</span>}
            </button>
          </nav>
        )}
        <span className="status" data-connection={status.state}>{status.state === "ok" ? status.text : status.state}</span>
      </header>
      {status.state === "error" && <p className="error">{status.text}</p>}
      {client && screen === "runs" && (
        <div className="layout">
          <div className="side">
            <NewRunForm client={client} onCreated={(id) => { setRunId(id); setCreated((n) => n + 1); }} />
            <RunsList client={client} selected={runId} onSelect={setRunId} refreshKey={created} />
          </div>
          {runId ? (
            <RunView key={runId} client={client} runId={runId} />
          ) : (
            <p className="muted">Start a run, or choose one from the list to see it here.</p>
          )}
        </div>
      )}
      {client && screen === "settings" && <SettingsView client={client} onWarnings={setWarnings} />}
    </main>
  );
}
