// The task board (B4): one card per stage from the run state, plus the latest
// test run and the budget. Model output is shown through SafeMarkdown (D29),
// collapsed by default.
import SafeMarkdown from "./SafeMarkdown.js";

export default function TaskBoard({ run }) {
  const byStage = new Map(run.agents.map((a) => [a.stage ?? a.agent, a]));
  return (
    <div className="board" aria-label="Task board">
      {run.stage_states.map((stage) => (
        <StageCard key={stage.stage} stage={stage} output={byStage.get(stage.stage)} />
      ))}
      <TestResult tests={run.tests} />
      <Budget budget={run.budget} />
    </div>
  );
}

function StageCard({ stage, output }) {
  return (
    <article className="stage-card" data-stage={stage.stage} data-status={stage.status}>
      <header>
        <strong>{stage.stage}</strong>
        <span className="stage-status" data-status={stage.status}>{stage.status}</span>
        {output?.target && <code className="muted">{output.target}</code>}
      </header>
      {output && (
        <dl className="facts">
          {output.attempts > 0 && <><dt>attempts</dt><dd>{output.attempts}</dd></>}
          {output.duration_ms != null && <><dt>time</dt><dd>{(output.duration_ms / 1000).toFixed(1)} s</dd></>}
          {Object.keys(output.tokens).length > 0 && (
            <><dt>tokens</dt><dd>{Object.entries(output.tokens).map(([k, v]) => `${k} ${v}`).join(", ")}</dd></>
          )}
          {output.verdict && <><dt>verdict</dt><dd data-field="verdict">{output.verdict}</dd></>}
          {output.run_command && <><dt>test command</dt><dd><code>{output.run_command}</code></dd></>}
          {output.files.length > 0 && <><dt>files</dt><dd>{output.files.join(", ")}</dd></>}
        </dl>
      )}
      {output?.notes.length > 0 && (
        <ul className="notes">{output.notes.map((note, i) => <li key={i}>{note}</li>)}</ul>
      )}
      {(stage.error || output?.error) && <p className="error">{stage.error || output.error}</p>}
      {output?.output && (
        <details>
          <summary>Output</summary>
          <SafeMarkdown text={output.output} />
        </details>
      )}
    </article>
  );
}

function TestResult({ tests }) {
  if (!tests) return null;
  const result = tests.timed_out ? "timed out" : tests.ok ? "passed" : `failed (exit ${tests.exit_code})`;
  return (
    <article className="stage-card" data-stage="tests" data-status={tests.ok ? "done" : "failed"}>
      <header>
        <strong>Tests</strong>
        <span className="stage-status" data-field="test-result" data-status={tests.ok ? "done" : "failed"}>{result}</span>
      </header>
      <pre className="verbatim">{tests.command}</pre>
      {["stdout", "stderr"].map((stream) => tests[stream] && (
        <details key={stream}>
          <summary>{stream}</summary>
          <pre className="verbatim output">{tests[stream]}</pre>
        </details>
      ))}
    </article>
  );
}

function Budget({ budget }) {
  const entries = Object.entries(budget);
  if (entries.length === 0) return null;
  return (
    <p className="muted budget">
      Budget used: {entries.map(([key, value]) => `${key.replaceAll("_", " ")} ${value}`).join(" · ")}
    </p>
  );
}
