// B1: the app shell. Gets { baseUrl, token } from the bridge, calls the
// token-guarded health endpoint, and shows the result. Screens start in B2.
import { useEffect, useState } from "react";

async function connect() {
  if (!window.sda) throw new Error("Open this app through the desktop shell (npm start or npm run dev).");
  const { baseUrl, token } = await window.sda.connection();
  const response = await fetch(`${baseUrl}/api/health`, { headers: { "X-API-Key": token } });
  if (!response.ok) throw new Error(`Health check failed: HTTP ${response.status}`);
  return response.json();
}

export default function App() {
  const [status, setStatus] = useState({ state: "connecting", text: "Connecting to the local API…" });

  useEffect(() => {
    let live = true;
    connect()
      .then((health) => live && setStatus({
        state: "ok",
        text: `Connected: API ${health.version} on port ${health.api_port}`,
      }))
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
    </main>
  );
}
