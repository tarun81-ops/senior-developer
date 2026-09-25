// The app shell: connect over the bridge (D26), check health, then the B2
// screen: a new-run form and the selected run, live.
import { useEffect, useState } from "react";

import { createClient } from "./api.js";
import NewRunForm from "./NewRunForm.jsx";
import RunView from "./RunView.jsx";

async function connect() {
  if (!window.sda) throw new Error("Open this app through the desktop shell (npm start or npm run dev).");
  const client = createClient(await window.sda.connection());
  return { client, health: await client.health() };
}

export default function App() {
  const [status, setStatus] = useState({ state: "connecting", text: "Connecting to the local API…" });
  const [client, setClient] = useState(null);
  const [runId, setRunId] = useState(null);

  useEffect(() => {
    let live = true;
    connect()
      .then(({ client: api, health }) => {
        if (!live) return;
        setClient(api);
        setStatus({ state: "ok", text: `Connected: API ${health.version} on port ${health.api_port}` });
      })
      .catch((err) => live && setStatus({ state: "error", text: String(err.message || err) }));
    return () => { live = false; };
  }, []);

  return (
    <main className="shell">
      <header>
        <h1>Senior Developer Agents</h1>
        <span className="status" data-connection={status.state}>{status.state === "ok" ? status.text : status.state}</span>
      </header>
      {status.state === "error" && <p className="error">{status.text}</p>}
      {client && (
        <div className="layout">
          <NewRunForm client={client} onCreated={setRunId} />
          {runId ? <RunView key={runId} client={client} runId={runId} /> : <p className="muted">Start a run to follow it here.</p>}
        </div>
      )}
    </main>
  );
}
