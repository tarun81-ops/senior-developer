// API client for the UI: token on every request, and the SSE stream read with
// fetch() because EventSource cannot send X-API-Key (D19). Plain ES module with
// an injectable fetch, so Node's test runner exercises it unchanged.

/** Kinds of the one closing event every run gets (backend/api/stream.py). */
export const CLOSING_KINDS = new Set(["api.run_succeeded", "api.run_failed", "api.run_cancelled"]);

export class ApiError extends Error {
  constructor(status, detail) {
    super(`${status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
    this.status = status;
    this.detail = detail;
  }
}

/**
 * Incremental SSE decoder. `push(text)` accepts arbitrary chunk boundaries and
 * calls `onMessage({ id, data })` per complete message; comment lines
 * (keepalives) are dropped. Per the SSE spec: CRLF/CR/LF line ends, multiple
 * `data:` lines joined by "\n", one optional space after the colon.
 */
export function createSseParser(onMessage) {
  let buffer = "";
  let data = [];
  let id = null;
  return {
    push(text) {
      buffer += text;
      // A trailing "\r" may be the first half of "\r\n": hold it back.
      const end = buffer.endsWith("\r") ? buffer.length - 1 : buffer.length;
      const lines = buffer.slice(0, end).split(/\r\n|\r|\n/);
      buffer = lines.pop() + buffer.slice(end); // the last piece may be partial
      for (const line of lines) {
        if (line === "") {
          if (data.length) onMessage({ id, data: data.join("\n") });
          data = [];
          id = null;
          continue;
        }
        if (line.startsWith(":")) continue;
        const colon = line.indexOf(":");
        const field = colon === -1 ? line : line.slice(0, colon);
        let value = colon === -1 ? "" : line.slice(colon + 1);
        if (value.startsWith(" ")) value = value.slice(1);
        if (field === "data") data.push(value);
        else if (field === "id") id = value;
      }
    },
  };
}

export function createClient({ baseUrl, token, fetch: fetchImpl = globalThis.fetch }) {
  const headers = (extra = {}) => ({ "X-API-Key": token, ...extra });

  async function request(method, path, body) {
    const response = await fetchImpl(baseUrl + path, {
      method,
      headers: headers(body === undefined ? {} : { "Content-Type": "application/json" }),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new ApiError(response.status, payload?.detail ?? payload);
    return payload;
  }

  /**
   * One stream connection from `cursor.seq`. Advances `cursor.seq` as each
   * event is delivered, so a connection that dies mid-way still leaves the
   * cursor at the last event the caller saw. Resolves true once the run's
   * closing event arrived, false if the connection just ended.
   */
  async function streamOnce(runId, cursor, onEvent, signal) {
    const query = new URLSearchParams({ run_id: runId, after_seq: String(cursor.seq) });
    const response = await fetchImpl(`${baseUrl}/api/events/stream?${query}`, {
      headers: headers({ Accept: "text/event-stream" }),
      signal,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      throw new ApiError(response.status, payload?.detail ?? payload);
    }
    let closed = false;
    const parser = createSseParser(({ data }) => {
      const event = JSON.parse(data);
      if (event.seq <= cursor.seq) return; // never deliver twice, whatever the server does
      cursor.seq = event.seq;
      if (CLOSING_KINDS.has(event.kind)) closed = true;
      onEvent(event);
    });
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      parser.push(value);
    }
    parser.push("\n\n"); // flush a final frame the server didn't terminate
    return closed;
  }

  /**
   * Follow a run to its end: stream, and on any drop reconnect with
   * exponential backoff from the last seq delivered, so the caller sees every
   * event exactly once. Resolves after the closing event; rejects on an HTTP
   * error the server means (401/404/400) or when `signal` aborts.
   */
  async function followRun(runId, onEvent, { afterSeq = -1, signal, backoff = {} } = {}) {
    const { initialMs = 250, maxMs = 5000, sleep = defaultSleep } = backoff;
    let delay = initialMs;
    const cursor = { seq: afterSeq };
    for (;;) {
      const before = cursor.seq;
      try {
        if (await streamOnce(runId, cursor, onEvent, signal)) return cursor.seq;
      } catch (err) {
        if (signal?.aborted || err instanceof ApiError) throw err;
        // network drop: retry from the cursor
      }
      if (cursor.seq > before) delay = initialMs; // progress resets the backoff
      await sleep(delay, signal);
      delay = Math.min(delay * 2, maxMs);
    }
  }

  return {
    health: () => request("GET", "/api/health"),
    createRun: (body) => request("POST", "/api/runs", body),
    listRuns: () => request("GET", "/api/runs"),
    getRun: (id) => request("GET", `/api/runs/${encodeURIComponent(id)}`),
    cancel: (id) => request("POST", `/api/runs/${encodeURIComponent(id)}/cancel`),
    approve: (id, note) => request("POST", `/api/runs/${encodeURIComponent(id)}/approve`, { note }),
    reject: (id, note) => request("POST", `/api/runs/${encodeURIComponent(id)}/reject`, { note }),
    getSettings: () => request("GET", "/api/settings"),
    saveSettings: (body) => request("PUT", "/api/settings", body),
    saveKeys: (keys) => request("PUT", "/api/settings/keys", { keys }),
    listFiles: (id) => request("GET", `/api/runs/${encodeURIComponent(id)}/files`),
    readFile: (id, path) =>
      request("GET", `/api/runs/${encodeURIComponent(id)}/files/content?${new URLSearchParams({ path })}`),
    followRun,
  };
}

function defaultSleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(timer);
      reject(signal.reason);
    }, { once: true });
  });
}
