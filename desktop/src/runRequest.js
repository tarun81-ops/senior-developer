// What a new-run form starts with and what it sends (D27). Plain JS so the
// rules are unit-tested without a DOM.

export const GATES = ["plan", "architecture", "execution"];

/**
 * Every new form starts from this, and the form resets to it after each
 * submit, so the execution gate is never remembered as off (D27 rule 1).
 */
export function freshForm() {
  return { request: "", project: "", gates: { plan: false, architecture: false, execution: true } };
}

/**
 * The POST /api/runs body. `modelOverride` is `{ provider, model }` or null;
 * only the development-only menu ever supplies one (D27 rule 2).
 */
export function buildRunRequest(form, modelOverride = null) {
  const request = form.request.trim();
  if (!request) throw new Error("Describe what the agents should build.");
  const body = {
    request,
    approval_gates: GATES.filter((gate) => form.gates[gate]),
  };
  const project = form.project.trim();
  if (project) body.project = project;
  if (modelOverride) Object.assign(body, modelOverride);
  return body;
}
