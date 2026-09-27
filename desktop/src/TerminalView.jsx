// A live PowerShell terminal in the run's own project folder (D43).
//
// This is a separate channel from everything else in the app: the agents'
// commands still only ever go through the sandboxed, allowlisted
// CommandRunner (they never see this tab or anything typed into it). This
// terminal is for the person using the app, exactly like opening a real
// PowerShell window in that folder — it exists purely for convenience.
//
// There is no real pseudo-terminal on the other end (see terminal.py), so
// this renders as plain scrolling text with one command at a time, not a
// full character-by-character emulator: no cursor-positioning, no color.
import { useCallback, useEffect, useRef, useState } from "react";

export default function TerminalView({ client, runId }) {
  const [lines, setLines] = useState([]); // [{ key, text, kind }]
  const [command, setCommand] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const inputRef = useRef(null);
  const bottomRef = useRef(null);

  const append = useCallback((text, kind = "output") => {
    if (!text) return;
    setLines((prev) => [...prev, { key: prev.length, text, kind }]);
  }, []);

  useEffect(() => {
    setLines([]);
    setError(null);
    let live = true;
    const abort = new AbortController();

    client
      .getTerminal(runId)
      .then((status) => {
        if (!live) return;
        setBusy(status.busy);
        if (status.start_error) setError(status.start_error);
      })
      .catch((err) => live && setError(err.message));

    client
      .followTerminal(
        runId,
        (chunk) => {
          if (chunk.text) append(chunk.text, chunk.closed ? "status" : "output");
          if (chunk.done) setBusy(false);
          if (chunk.closed) setBusy(false);
        },
        { signal: abort.signal }
      )
      .catch((err) => {
        if (!abort.signal.aborted) setError(err.message);
      });

    return () => {
      live = false;
      abort.abort();
    };
  }, [client, runId, append]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "nearest" });
  }, [lines.length]);

  const run = useCallback(
    async (event) => {
      event.preventDefault();
      const text = command.trim();
      if (!text || busy) return;
      setError(null);
      append(`PS> ${text}\n`, "echo");
      setCommand("");
      setBusy(true);
      try {
        await client.sendTerminalCommand(runId, text);
      } catch (err) {
        setBusy(false);
        setError(err.message);
      }
    },
    [client, runId, command, busy, append]
  );

  async function stop() {
    try {
      await client.interruptTerminal(runId);
    } catch (err) {
      setError(err.message);
    }
  }

  async function restart() {
    try {
      await client.restartTerminal(runId);
      setLines([]);
      setBusy(false);
      inputRef.current?.focus();
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <div className="terminal" aria-label="Terminal">
      <div className="terminal-head">
        <span className="muted">PowerShell, in this run's project folder</span>
        <span className="spacer" />
        {busy && (
          <button type="button" onClick={stop} title="Send Ctrl+Break to the running command">
            Stop
          </button>
        )}
        <button type="button" onClick={restart} title="Kill this shell; the next command starts a fresh one">
          Restart shell
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      <pre className="terminal-output" aria-label="Terminal output" data-field="terminal-output">
        {lines.map((line) => (
          <span key={line.key} data-kind={line.kind}>
            {line.text}
          </span>
        ))}
        <div ref={bottomRef} />
      </pre>
      <form className="terminal-input" onSubmit={run}>
        <span className="prompt">PS&gt;</span>
        <input
          ref={inputRef}
          value={command}
          disabled={busy}
          onChange={(event) => setCommand(event.target.value)}
          placeholder={busy ? "Running…" : "Type a PowerShell command"}
          autoComplete="off"
          spellCheck={false}
          aria-label="PowerShell command"
        />
        <button type="submit" disabled={busy || !command.trim()}>
          Run
        </button>
      </form>
    </div>
  );
}
