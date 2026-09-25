// Development-only menu (D27 rule 2). App renders it only under
// `import.meta.env.DEV`, so a production build drops this module entirely,
// including the mock model's name (a build test checks the bundle).
const MOCK_MODEL = { provider: "mock", model: "mock-echo" };

export default function DevMenu({ override, onChange }) {
  return (
    <details className="dev-menu">
      <summary>Developer</summary>
      <label>
        <input
          type="checkbox"
          checked={override !== null}
          onChange={(e) => onChange(e.target.checked ? MOCK_MODEL : null)}
        />{" "}
        Use the offline mock model (no network, no quota)
      </label>
    </details>
  );
}
