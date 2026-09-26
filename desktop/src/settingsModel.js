// Settings screen rules (B5, D31). Plain JS so they are unit-tested without a DOM.

/** Providers a person may pick. The offline mock only in development (D27). */
export function pickableProviders(providers, { dev = false } = {}) {
  return providers.filter((p) => dev || p.kind !== "mock");
}

export const pinKey = (pin) => (pin ? `${pin.provider}/${pin.model}` : "");

/**
 * Choices for an agent's first-choice model. An existing pin that is not in
 * the list (e.g. the mock, pinned in development) stays listed, so opening
 * and saving the screen never changes a setting you did not touch.
 */
export function modelOptions(providers, currentPin, { dev = false } = {}) {
  const options = pickableProviders(providers, { dev }).flatMap((p) =>
    p.models.map((model) => ({ value: `${p.provider}/${model}`, provider: p.provider, model, label: `${p.label}: ${model}` })),
  );
  const current = pinKey(currentPin);
  if (current && !options.some((o) => o.value === current)) {
    options.push({ value: current, ...currentPin, label: `${current} (not normally offered)` });
  }
  return options;
}

/** The editable copy of the saved settings. */
export function draftFrom(settings, { dev = false } = {}) {
  const named = settings.provider_order;
  const rest = pickableProviders(settings.providers, { dev })
    .map((p) => p.provider)
    .filter((name) => !named.includes(name));
  return {
    pins: Object.fromEntries(settings.agents.map((a) => [a.agent, a.override ? { ...a.override } : null])),
    order: [...named, ...rest],
    orderTouched: false,
  };
}

/** Move one entry up (-1) or down (+1); returns a new list. */
export function move(list, index, delta) {
  const to = index + delta;
  if (to < 0 || to >= list.length) return list;
  const next = [...list];
  [next[index], next[to]] = [next[to], next[index]];
  return next;
}

/**
 * The PUT /api/settings body: every pin, and the provider order only if it
 * was changed here. An untouched order keeps what was saved, which may be
 * empty (the tracked order).
 */
export function updateFrom(draft, settings) {
  const agents = {};
  for (const [agent, pin] of Object.entries(draft.pins)) {
    if (pin) agents[agent] = { provider: pin.provider, model: pin.model };
  }
  return { agents, provider_order: draft.orderTouched ? draft.order : settings.provider_order };
}

/** Only keys that were typed; values are never kept once sent. */
export function keysToSend(entered) {
  return Object.fromEntries(
    Object.entries(entered).map(([name, value]) => [name, value.trim()]).filter(([, value]) => value),
  );
}

/** Web research (D42) and deploy credentials (D41), shown apart from the model providers' keys. */
export const RESEARCH_KEYS = ["FIRECRAWL_API_KEY"];
export const KEY_HELP = {
  FIRECRAWL_API_KEY: "optional; lets every agent search the web (firecrawl.dev → API keys)",
  GITHUB_TOKEN: "fine-grained token: Administration, Contents, Pages, Workflows (read/write), Actions (read)",
  RENDER_API_KEY: "Render account settings → API keys",
  RENDER_OWNER_ID: "optional; only if your Render key reaches several workspaces",
};

/** Split key names into [model provider keys, research keys, deploy keys]. */
export function keyGroups(names) {
  const research = names.filter((n) => RESEARCH_KEYS.includes(n));
  const deploy = names.filter((n) => n in KEY_HELP && !research.includes(n));
  return [names.filter((n) => !(n in KEY_HELP)), research, deploy];
}
