// The Deploy tab (Phase 5, P5.6; D35-D41). Shows where a finished run would
// go and why it can't yet, lets a person review exactly what will be
// published, and follows the deploy live on its own stream. The resulting
// URL is text to copy, never a link: the page has no way to open one (D26).
import { useCallback, useEffect, useRef, useState } from "react";

import { CLOSING_KINDS } from "./api.js";

const TARGET_NAMES = { "github-pages": "GitHub Pages", render: "Render" };

export default function DeployView({ client, runId, refreshKey }) {
  const [preview, setPreview] = useState(null);
  const [deploys, setDeploys] = useState([]);
  const [error, setError] = useState(null);
  const [reviewing, setReviewing] = useState(false);
  const [active, setActive] = useState(null); // { id, events: [] }

  const load = useCallback(() => {
    setError(null);
    client.deployPreview(runId).then(setPreview).catch((err) => setError(err.message));
    client.listDeploys(runId).then((body) => setDeploys(body.deploys)).catch(() => {});
  }, [client, runId]);

  useEffect(load, [load, refreshKey]);

  // follow the active deploy on its own stream until its closing event
  useEffect(() => {
    if (!active?.id) return undefined;
    const abort = new AbortController();
    client
      .followRun(active.id, (event) => {
        setActive((prev) => (prev && prev.id === active.id ? { ...prev, events: [...prev.events, event] } : prev));
      }, { signal: abort.signal })
      .then(load)
      .catch((err) => { if (!abort.signal.aborted) setError(err.message); });
    return () => abort.abort();
  }, [client, active?.id, load]);

  if (error && !preview) return <p className="error">{error}</p>;
  if (!preview) return <p className="muted">Checking whether this run can be deployed…</p>;

  const target = preview.target;
  const closing = active?.events.find((e) => CLOSING_KINDS.has(e.kind));
  const running = active && !closing;

  return (
    <div className="deploy" aria-label="Deploy">
      <p>
        {target.target ? (
          <>Target: <strong>{TARGET_NAMES[target.target]}</strong>, from a <strong>{preview.visibility}</strong>{" "}
            GitHub repo <code>{preview.repo}</code>. <span className="muted">({target.reason})</span></>
        ) : (
          <span className="muted">No deploy target: {target.reason}.</span>
        )}
      </p>

      {preview.blockers.length > 0 ? (
        <div className="notice" data-field="blockers">
          <strong>Not ready to deploy:</strong>
          <ul>{preview.blockers.map((b) => <li key={b}>{b}</li>)}</ul>
        </div>
      ) : (
        !running && (
          <button type="button" className="approve" onClick={() => setReviewing(true)}>
            Review and deploy…
          </button>
        )
      )}
      {error && <p className="error">{error}</p>}

      {active && (
        <section className="deploy-progress" aria-label="Deploy progress">
          <h3>{running ? "Deploying…" : closing.kind === "deploy.succeeded" ? "Deployed" : "Deploy failed"}</h3>
          <ol className="deploy-events">
            {active.events.map((e) => (
              <li key={e.seq} data-kind={e.kind}><code>{e.kind}</code> {e.kind === "deploy.succeeded" ? "" : e.message}</li>
            ))}
          </ol>
          {closing?.kind === "deploy.succeeded" && <CopyableUrl url={closing.message} />}
          {closing?.kind === "deploy.failed" && <p className="error" data-field="deploy-error">{closing.message}</p>}
        </section>
      )}

      {deploys.length > 0 && (
        <section aria-label="Deploy history">
          <h3>History</h3>
          <ul className="deploy-history">
            {deploys.map((d) => (
              <li key={d.deploy_id} data-status={d.status}>
                <span className="status-pill" data-status={d.status}>{d.status}</span>{" "}
                <span className="muted">{new Date(d.created_at).toLocaleString()}</span>{" "}
                {d.url ? <code>{d.url}</code> : d.error ? <span className="error">{d.error}</span> : null}
              </li>
            ))}
          </ul>
        </section>
      )}

      {reviewing && (
        <DeployDialog
          client={client}
          runId={runId}
          preview={preview}
          onClose={() => setReviewing(false)}
          onStarted={(id) => { setReviewing(false); setActive({ id, events: [] }); }}
          onStale={load}
        />
      )}
    </div>
  );
}

function DeployDialog({ client, runId, preview, onClose, onStarted, onStale }) {
  const dialog = useRef(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const target = preview.target;
  const isPublic = preview.visibility === "public";

  useEffect(() => { dialog.current.showModal(); }, []);

  async function publish() {
    setBusy(true);
    setError(null);
    try {
      const started = await client.deploy(runId, preview.fingerprint);
      onStarted(started.deploy_id);
    } catch (err) {
      setError(err.message);
      if (/changed since the preview/.test(err.message)) onStale();
    } finally {
      setBusy(false);
    }
  }

  // same guard as the approval dialog (D31): a queued close event only
  // counts if the dialog is still closed when it arrives
  const handleCloseEvent = () => { if (!dialog.current?.open) onClose(); };

  return (
    <dialog ref={dialog} className="approval deploy-dialog" aria-labelledby="deploy-title" onClose={handleCloseEvent}>
      <h2 id="deploy-title">Publish {preview.repo} to {TARGET_NAMES[target.target]}?</h2>
      <p className="warning" data-field="visibility-warning">
        {isPublic
          ? "This publishes the project to a PUBLIC GitHub repository and a public website. Anyone on the internet can see the site and its source code, and copies can outlive a later delete."
          : "The source goes to a PRIVATE GitHub repository, but the running service at its URL is public on the internet."}
      </p>
      <dl className="facts">
        <dt>Repository</dt><dd><code>{preview.repo}</code> ({preview.visibility})</dd>
        <dt>Address</dt><dd><code>{preview.expected_url}</code></dd>
        {target.build_command && <><dt>Build</dt><dd><pre className="verbatim">{target.build_command}</pre></dd></>}
        {target.start_command && <><dt>Start</dt><dd><pre className="verbatim">{target.start_command}</pre></dd></>}
      </dl>
      <h3>{preview.files.length} files will be published ({formatSize(preview.total_bytes)})</h3>
      <ul className="publish-files" data-field="files">
        {preview.files.map((f) => <li key={f.path}><code>{f.path}</code> <span className="muted">{formatSize(f.size)}</span></li>)}
      </ul>
      {preview.excluded.length > 0 && (
        <details>
          <summary>{preview.excluded.length} files are not published</summary>
          <ul>{preview.excluded.map((f) => <li key={f.path}><code>{f.path}</code> <span className="muted">{f.reason}</span></li>)}</ul>
        </details>
      )}
      {error && <p className="error">{error}</p>}
      <div className="actions">
        {/* Cancel first: the dialog focuses it, so Enter never publishes by accident */}
        <button type="button" onClick={() => dialog.current.close()} disabled={busy}>Cancel</button>
        <span className="spacer" />
        <button type="button" className="approve" onClick={publish} disabled={busy}>
          {busy ? "Starting…" : target.target === "render" ? "Deploy to Render" : "Publish to GitHub Pages"}
        </button>
      </div>
    </dialog>
  );
}

function CopyableUrl({ url }) {
  const input = useRef(null);
  const [copied, setCopied] = useState(false);
  const copy = () => {
    input.current.select();
    // no clipboard permission is granted to the page (D26); a user-initiated
    // copy of selected text still works
    setCopied(document.execCommand("copy"));
  };
  return (
    <p className="copy-url">
      Live at{" "}
      <input ref={input} readOnly value={url} data-field="deploy-url" size={Math.min(url.length, 60)}
        onFocus={(e) => e.target.select()} />{" "}
      <button type="button" onClick={copy}>{copied ? "Copied" : "Copy"}</button>
    </p>
  );
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
