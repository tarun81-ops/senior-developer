// Read-only file viewer (B4, D30): what is actually on disk in the run's
// project folder, and one file at a time as plain text. File contents are
// model-written, so they are only ever shown as text in a <pre>, never as
// markdown or HTML, whatever the file's extension.
import { useCallback, useEffect, useState } from "react";

export default function FilesView({ client, runId, refreshKey }) {
  const [listing, setListing] = useState(null);
  const [selected, setSelected] = useState(null);
  const [file, setFile] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(() => {
    setError(null);
    client.listFiles(runId).then(setListing).catch((err) => setError(err.message));
  }, [client, runId]);

  // reload when the run moves on (refreshKey is the run's status)
  useEffect(load, [load, refreshKey]);

  useEffect(() => {
    if (!selected) return undefined;
    let live = true;
    setFile(null);
    client.readFile(runId, selected)
      .then((body) => live && setFile(body))
      .catch((err) => live && setFile({ error: err.message }));
    return () => { live = false; };
  }, [client, runId, selected]);

  if (error) return <p className="error">{error}</p>;
  if (!listing) return <p className="muted">Loading files…</p>;
  return (
    <div className="files" aria-label="Files">
      <div className="file-list">
        <div className="row">
          <span className="muted">workspace/{listing.project}/</span>
          <button type="button" onClick={load}>Refresh</button>
        </div>
        {listing.files.length === 0 ? (
          <p className="muted">No files yet.</p>
        ) : (
          <ul>
            {listing.files.map((f) => (
              <li key={f.path}>
                <button
                  type="button"
                  className="file-link"
                  aria-current={f.path === selected ? "true" : undefined}
                  onClick={() => setSelected(f.path)}
                >
                  <span>{f.path}</span>
                  <span className="muted">{formatSize(f.size)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
        {listing.truncated && <p className="muted">Only the first {listing.files.length} files are listed.</p>}
      </div>
      <div className="file-view">
        {!selected && <p className="muted">Choose a file to view it (read-only).</p>}
        {selected && !file && <p className="muted">Loading {selected}…</p>}
        {file?.error && <p className="error">{file.error}</p>}
        {file && !file.error && (
          <>
            <p><code>{file.path}</code> <span className="muted">{formatSize(file.size)}</span></p>
            {file.binary ? (
              <p className="notice">Binary file, not shown.</p>
            ) : (
              <pre className="verbatim code" data-field="file-content">{file.content}</pre>
            )}
            {file.truncated && <p className="notice">Only the first part of this large file is shown.</p>}
          </>
        )}
      </div>
    </div>
  );
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
