// Settings (B5, D22, D31): first-choice model per agent, provider order, and
// API keys. Keys are write-only: typed into password fields, sent once,
// cleared, and only ever shown back as "set" or "missing".
import { useEffect, useState } from "react";

import { KEY_HELP, draftFrom, keyGroups, keysToSend, modelOptions, move, pinKey, updateFrom } from "./settingsModel.js";

const DEV = import.meta.env.DEV;

export default function SettingsView({ client, onWarnings }) {
  const [settings, setSettings] = useState(null);
  const [error, setError] = useState(null);
  // Kept here, not in ModelSettings: that remounts from each saved result so
  // its draft matches what was saved, and the message must survive that.
  const [modelStatus, setModelStatus] = useState(null);

  const apply = (body) => {
    setSettings(body);
    onWarnings(body.warnings);
  };

  useEffect(() => {
    client.getSettings()
      .then((body) => { setSettings(body); onWarnings(body.warnings); })
      .catch((err) => setError(err.message));
  }, [client, onWarnings]);

  if (error) return <p className="error">{error}</p>;
  if (!settings) return <p className="muted">Loading settings…</p>;
  return (
    <div className="settings">
      {settings.warnings.map((warning) => (
        <p key={warning} className="warning" role="alert">
          <strong>Your saved settings were ignored.</strong> {warning} Saving below replaces that file;
          {" "}<em>Reset to defaults</em> removes the saved choices.
        </p>
      ))}
      <ModelSettings
        key={JSON.stringify(settings)}
        client={client}
        settings={settings}
        onSaved={apply}
        status={modelStatus}
        setStatus={setModelStatus}
      />
      <KeySettings client={client} settings={settings} onSaved={apply} />
    </div>
  );
}

function ModelSettings({ client, settings, onSaved, status, setStatus }) {
  const [draft, setDraft] = useState(() => draftFrom(settings, { dev: DEV }));
  const labels = Object.fromEntries(settings.providers.map((p) => [p.provider, p.label]));

  async function save(body) {
    setStatus({ busy: true, n: status?.n ?? 0 });
    try {
      onSaved(await client.saveSettings(body));
      setStatus({ ok: "Saved. New runs use these settings; runs already started keep theirs.", n: (status?.n ?? 0) + 1 });
    } catch (err) {
      setStatus({ error: err.message, n: (status?.n ?? 0) + 1 });
    }
  }

  const setPin = (agent, value, options) => {
    const option = options.find((o) => o.value === value);
    setDraft({ ...draft, pins: { ...draft.pins, [agent]: option ? { provider: option.provider, model: option.model } : null } });
  };
  const reorder = (index, delta) => setDraft({ ...draft, order: move(draft.order, index, delta), orderTouched: true });

  return (
    // data-saves counts finished saves, so a reader (or the smoke test) can
    // tell a new result from the previous one
    <section className="card" aria-label="Models" data-saves={status?.n ?? 0}>
      <h2>Models</h2>
      <p className="muted">
        Pick the model each agent tries first. Its configured fallbacks stay behind it, so a model
        that is out of quota still fails over.
      </p>
      <table className="agents">
        <thead><tr><th>Agent</th><th>First choice</th><th>Chain the next run will use</th></tr></thead>
        <tbody>
          {settings.agents.map((agent) => {
            const options = modelOptions(settings.providers, draft.pins[agent.agent], { dev: DEV });
            return (
              <tr key={agent.agent} data-agent={agent.agent}>
                <td>{agent.agent}</td>
                <td>
                  <select
                    name={`pin-${agent.agent}`}
                    value={pinKey(draft.pins[agent.agent])}
                    onChange={(e) => setPin(agent.agent, e.target.value, options)}
                  >
                    <option value="">Configured default</option>
                    {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                </td>
                <td className="chain" data-field="chain">
                  {agent.routing.map((r) => `${r.provider}/${r.model}`).join(" → ")}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <h3>Provider order</h3>
      <p className="muted">Within each agent's chain, try these providers in this order.</p>
      <ol className="provider-order">
        {draft.order.map((name, i) => (
          <li key={name} data-provider={name}>
            <span>{labels[name] ?? name}</span>
            <button type="button" aria-label={`Move ${name} up`} onClick={() => reorder(i, -1)} disabled={i === 0}>↑</button>
            <button type="button" aria-label={`Move ${name} down`} onClick={() => reorder(i, 1)} disabled={i === draft.order.length - 1}>↓</button>
          </li>
        ))}
      </ol>

      {status?.error && <p className="error">{status.error}</p>}
      {status?.ok && <p className="ok">{status.ok}</p>}
      <div className="actions">
        <button type="button" onClick={() => save({})} disabled={status?.busy}>Reset to defaults</button>
        <button type="button" className="approve" onClick={() => save(updateFrom(draft, settings))} disabled={status?.busy}>
          Save model settings
        </button>
      </div>
    </section>
  );
}

function KeySettings({ client, settings, onSaved }) {
  const [entered, setEntered] = useState({});
  const [status, setStatus] = useState(null);
  const [providerKeys, researchKeys, deployKeys] = keyGroups(Object.keys(settings.keys));
  const row = (name) => (
    <label key={name} className="key-row" data-key={name}>
      <code>{name}</code>
      <span className="key-status" data-status={settings.keys[name]}>{settings.keys[name]}</span>
      <input
        type="password"
        name={`key-${name}`}
        value={entered[name] ?? ""}
        onChange={(e) => setEntered({ ...entered, [name]: e.target.value })}
        placeholder={settings.keys[name] === "set" ? "replace" : "paste"}
        autoComplete="new-password"
        spellCheck={false}
      />
      {KEY_HELP[name] && <span className="muted key-help">{KEY_HELP[name]}</span>}
    </label>
  );

  async function save(event) {
    event.preventDefault();
    const keys = keysToSend(entered);
    setEntered({}); // never keep a key in the page longer than the request
    if (Object.keys(keys).length === 0) return;
    const n = (status?.n ?? 0) + 1;
    setStatus({ busy: true, n: n - 1 });
    try {
      onSaved(await client.saveKeys(keys));
      const saved = Object.keys(keys);
      setStatus({ ok: `Saved ${saved.join(", ")} to .env. New runs use ${saved.length > 1 ? "them" : "it"}.`, n });
    } catch (err) {
      setStatus({ error: err.message, n });
    }
  }

  return (
    <form className="card" aria-label="API keys" onSubmit={save} autoComplete="off" data-saves={status?.n ?? 0}>
      <h2>API keys</h2>
      <p className="muted">
        Keys are saved to <code>.env</code> on this computer. They are never shown again, only
        whether each one is set.
      </p>
      <h3>Model providers</h3>
      {providerKeys.map(row)}
      <h3>Web research (Firecrawl)</h3>
      {researchKeys.map(row)}
      <h3>Deploying (GitHub Pages, Render)</h3>
      {deployKeys.map(row)}
      {status?.error && <p className="error">{status.error}</p>}
      {status?.ok && <p className="ok">{status.ok}</p>}
      <div className="actions">
        <button type="submit" disabled={status?.busy}>Save keys</button>
      </div>
    </form>
  );
}
