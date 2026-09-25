// node --test: the SSE parser and the follow/reconnect loop, no server needed.
import assert from "node:assert/strict";
import { test } from "node:test";

import { ApiError, createClient, createSseParser } from "../src/api.js";

function parseAll(chunks) {
  const out = [];
  const parser = createSseParser((m) => out.push(m));
  for (const chunk of chunks) parser.push(chunk);
  return out;
}

test("parser handles any chunk boundary, CRLF, comments and multi-line data", () => {
  const text = 'id: 0\r\ndata: {"a":1}\r\n\r\n: keepalive 123\n\nid: 1\ndata: line1\ndata:line2\n\n';
  const expected = [
    { id: "0", data: '{"a":1}' },
    { id: "1", data: "line1\nline2" },
  ];
  assert.deepEqual(parseAll([text]), expected);
  // every possible split point, including between "\r" and "\n"
  for (let i = 1; i < text.length; i++) {
    assert.deepEqual(parseAll([text.slice(0, i), text.slice(i)]), expected, `split at ${i}`);
  }
  // one character at a time
  assert.deepEqual(parseAll([...text]), expected);
});

const event = (seq, kind = "stage.start") => ({ seq, kind, run_id: "r1", message: "" });
const frame = (e) => `id: ${e.seq}\ndata: ${JSON.stringify(e)}\n\n`;

/** A fake fetch that serves one scripted stream per connection. */
function fakeFetch(connections) {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url: new URL(url), headers: init.headers });
    const script = connections.shift();
    if (script.status) {
      return new Response(JSON.stringify({ detail: "nope" }), { status: script.status });
    }
    const chunks = [...script.chunks];
    const body = new ReadableStream({
      // one chunk per read, so a drop happens after the reader saw the rest
      pull(controller) {
        if (chunks.length) controller.enqueue(new TextEncoder().encode(chunks.shift()));
        else if (script.drop) controller.error(new TypeError("network error"));
        else controller.close();
      },
    });
    return new Response(body, { status: 200 });
  };
  return { fetch, calls };
}

test("followRun resumes from the last seq after a drop: no gap, no repeat", async () => {
  const all = [0, 1, 2, 3, 4].map((s) => event(s));
  all.push(event(5, "api.run_succeeded"));
  const { fetch, calls } = fakeFetch([
    // first connection drops mid-frame after seq 2
    { chunks: [frame(all[0]), frame(all[1]), frame(all[2]), frame(all[3]).slice(0, 10)], drop: true },
    // a clean end without the closing event (server restart, say)
    { chunks: [frame(all[3])] },
    // the server resends seq 3 by mistake; the client must not deliver it twice
    { chunks: [frame(all[3]), frame(all[4]), ": keepalive\n\n", frame(all[5])] },
  ]);
  const sleeps = [];
  const client = createClient({ baseUrl: "http://127.0.0.1:1", token: "t", fetch });
  const seen = [];
  const last = await client.followRun("r1", (e) => seen.push(e.seq), {
    backoff: { initialMs: 100, maxMs: 150, sleep: async (ms) => { sleeps.push(ms); } },
  });

  assert.deepEqual(seen, [0, 1, 2, 3, 4, 5]);
  assert.equal(last, 5);
  assert.deepEqual(calls.map((c) => c.url.searchParams.get("after_seq")), ["-1", "2", "3"]);
  assert.ok(calls.every((c) => c.headers["X-API-Key"] === "t"), "token on every request");
  // progress resets the backoff: both retries wait the initial delay
  assert.deepEqual(sleeps, [100, 100]);
});

test("followRun backs off exponentially while nothing arrives", async () => {
  const { fetch } = fakeFetch([
    { chunks: [], drop: true },
    { chunks: [], drop: true },
    { chunks: [], drop: true },
    { chunks: [], drop: true },
    { chunks: [frame(event(0, "api.run_cancelled"))] },
  ]);
  const sleeps = [];
  const client = createClient({ baseUrl: "http://x", token: "t", fetch });
  await client.followRun("r1", () => {}, {
    backoff: { initialMs: 100, maxMs: 500, sleep: async (ms) => { sleeps.push(ms); } },
  });
  assert.deepEqual(sleeps, [100, 200, 400, 500]);
});

test("followRun stops on an HTTP error instead of retrying forever", async () => {
  const { fetch, calls } = fakeFetch([{ status: 401 }]);
  const client = createClient({ baseUrl: "http://x", token: "bad", fetch });
  await assert.rejects(client.followRun("r1", () => {}), (err) => err instanceof ApiError && err.status === 401);
  assert.equal(calls.length, 1);
});
