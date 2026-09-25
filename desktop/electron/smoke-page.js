// Evaluated inside the real page by `electron . --smoke` (main.cjs). Drives
// the UI the way a person would and returns a report; main.cjs decides
// pass/fail. Uses the offline mock model from the development-only menu, so
// nothing leaves the machine.
(async () => {
  const report = { steps: [] };
  const step = (name, ok, detail) => {
    report.steps.push({ name, ok: Boolean(ok), ...(detail === undefined ? {} : { detail }) });
    if (!ok) throw new Error(`step failed: ${name}`);
  };
  const until = (find, what, ms = 30000) => new Promise((resolve, reject) => {
    const started = Date.now();
    (function poll() {
      let found;
      try { found = find(); } catch { found = null; }
      if (found) resolve(found);
      else if (Date.now() - started > ms) reject(new Error(`timed out waiting for ${what}`));
      else setTimeout(poll, 50);
    })();
  });
  const $ = (selector) => document.querySelector(selector);
  const button = (text) => [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === text);
  const checkbox = (name) => $(`input[name="${name}"]`);
  // React tracks input values itself: set through the native setter, then fire input
  const type = (el, value) => {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  };
  const setChecked = (el, want) => { if (el.checked !== want) el.click(); };
  const pill = () => $(".run .status-pill")?.dataset.status; // the run view, not the runs list

  try {
    // -- B1: connection and the locked-down page (D26) ----------------------
    const status = await until(() => {
      const s = $("[data-connection]")?.dataset.connection;
      return s === "ok" || s === "error" ? s : null;
    }, "the connection");
    step("connected", status === "ok", $("[data-connection]").textContent);
    step("origin is sda://app", location.origin === "sda://app", location.origin);
    step("no Node in the page", typeof require === "undefined" && typeof process === "undefined");
    step("bridge is exactly connection()", JSON.stringify(Object.keys(window.sda)) === '["connection"]');
    let violated = null;
    document.addEventListener("securitypolicyviolation", (e) => { violated = e.violatedDirective; });
    await fetch("http://127.0.0.1:1/").catch(() => {}); // loopback port 1: nothing real is contacted
    await new Promise((r) => setTimeout(r, 100));
    step("CSP blocks other origins", violated === "connect-src", violated);
    // the smoke root starts with a corrupt data/settings.local.yaml (main.cjs)
    step("Settings is flagged from startup when saved settings were ignored",
      Boolean(await until(() => $(".screens .badge"), "the settings badge")));

    // -- B2: the form's defaults (D27) ---------------------------------------
    step("execution gate on by default", checkbox("gate-execution").checked);
    step("plan/architecture gates off by default",
      !checkbox("gate-plan").checked && !checkbox("gate-architecture").checked);
    const mock = await until(() => [...document.querySelectorAll(".dev-menu input")][0], "the dev menu");
    setChecked(mock, true);

    // -- B2: a run followed live to success ----------------------------------
    type($("textarea[name=request]"), "smoke: a tiny calculator");
    setChecked(checkbox("gate-execution"), false);
    button("Start run").click();
    await until(() => pill() === "succeeded" || pill() === "failed", "run 1 to finish");
    step("run 1 succeeded", pill() === "succeeded", $(".run .error")?.textContent);
    const rows = [...document.querySelectorAll(".events tr")];
    const kinds = rows.map((r) => r.dataset.kind);
    step("events streamed live", rows.length > 5 && kinds[0] === "api.run_queued", rows.length);
    step("log ends with the closing event", kinds.at(-1) === "api.run_succeeded", kinds.at(-1));
    const stages = [...document.querySelectorAll(".timeline li")].map((li) => li.dataset.status);
    step("every stage done in the timeline", stages.length > 0 && stages.every((s) => s === "done"), stages);
    step("form reset: execution gate back on", checkbox("gate-execution").checked);
    step("form reset: request cleared", $("textarea[name=request]").value === "");

    // -- B2: cancel a run waiting at a gate ------------------------------------
    type($("textarea[name=request]"), "smoke: cancel me");
    setChecked(checkbox("gate-plan"), true);
    button("Start run").click();
    await until(() => pill() === "waiting_approval", "run 2 to wait at the plan gate");
    step("waiting run explains itself", /plan/.test($(".notice")?.textContent || ""));
    button("Cancel run").click();
    await until(() => pill() === "cancelled", "run 2 to be cancelled");
    step("cancel ends the run", pill() === "cancelled");
    await until(() => [...document.querySelectorAll(".events tr")].at(-1)?.dataset.kind === "api.run_cancelled",
      "the closing event");
    step("cancel button gone once finished", !button("Cancel run"));

    // -- B3: every approval dialog, decided through the UI --------------------
    const dialog = () => $("dialog.approval[open]");
    const waitDialog = (gate) => until(() => {
      const d = dialog();
      return d && d.querySelector("h2").textContent.toLowerCase().includes(gate) ? d : null;
    }, `the ${gate} dialog`);
    const inDialog = (text) => [...dialog().querySelectorAll("button")].find((b) => b.textContent.trim() === text);
    const inert = (d) => !d.querySelector("a, img, script, iframe, [href], [src]");

    type($("textarea[name=request]"), "smoke: all three gates");
    type($("input[name=project]"), "smoke-gates");
    setChecked(checkbox("gate-plan"), true);
    setChecked(checkbox("gate-architecture"), true);
    step("execution gate still on for this run", checkbox("gate-execution").checked);
    button("Start run").click();

    let d = await waitDialog("plan");
    step("plan dialog shows the stage output as markdown", d.querySelector(".markdown")?.textContent.includes("MOCK OK"));
    step("plan dialog content is inert (no links, images, scripts)", inert(d));
    step("the note field has focus, not Approve", document.activeElement === d.querySelector("textarea[name=approval-note]"));
    inDialog("Decide later").click();
    await until(() => !dialog(), "the dialog to close");
    step("'Decide later' leaves the run waiting", pill() === "waiting_approval");
    button("Review and decide").click();
    d = await waitDialog("plan");
    type(d.querySelector("textarea[name=approval-note]"), "plan looks fine");
    inDialog("Approve").click();

    d = await waitDialog("architecture");
    step("architecture dialog opens next", /architecture/i.test(d.querySelector("h2").textContent));
    inDialog("Approve").click();

    d = await waitDialog("command");
    const field = (name) => d.querySelector(`[data-field="${name}"]`)?.textContent;
    step("execution dialog shows the exact command", field("command") === "python -m pytest -q", field("command"));
    step("execution dialog shows the working folder",
      /[\\/]workspace[\\/]smoke-gates$/.test(field("cwd") || ""), field("cwd"));
    step("execution dialog shows the time limit", /^300 seconds/.test(field("timeout") || ""), field("timeout"));
    step("execution command is not rendered as markdown", !d.querySelector(".markdown"));
    await new Promise((r) => setTimeout(r, 600)); // a person reads first (and a screenshot can be taken)
    type(d.querySelector("textarea[name=approval-note]"), "not on my machine");
    inDialog("Reject").click();
    await until(() => pill() === "failed", "the rejected run to fail");
    step("reject fails the run with the note", /execution/.test($(".run .error")?.textContent || "")
      && /not on my machine/.test($(".run .error").textContent), $(".run .error")?.textContent);
    const kinds3 = [...document.querySelectorAll(".events tr")].map((r) => r.dataset.kind);
    step("the log records both approvals and the rejection",
      kinds3.filter((k) => k === "api.run_approved").length === 2 && kinds3.includes("api.run_rejected"));
    step("no command ran", !kinds3.includes("exec.start"));

    // -- B4: task board and read-only file viewer ------------------------------
    const tab = (label) => [...document.querySelectorAll(".tabs button")].find((b) => b.textContent === label);
    tab("Task board").click();
    const planner = await until(() => $('.stage-card[data-stage="planner"]'), "the task board");
    step("board shows each stage with its status", planner.dataset.status === "done"
      && $('.stage-card[data-stage="tester"]')?.dataset.status === "done", planner.dataset.status);
    step("board shows which model answered", /mock\/mock-echo/.test(planner.textContent));
    planner.querySelector("details summary").click();
    step("stage output opens as sanitized markdown", planner.querySelector("details .markdown")?.textContent.includes("MOCK OK"));
    step("board content is inert", !$(".board").querySelector("a, img, script, iframe, [href], [src]"));

    tab("Files").click();
    const listed = await until(() => {
      const names = [...document.querySelectorAll(".file-link")].map((b) => b.textContent);
      return names.length ? names : null;
    }, "the file list");
    step("files list what is on disk", listed.includes("tests/test_ok.py") && listed.includes("notes.md"), listed);
    const open = async (name) => {
      [...document.querySelectorAll(".file-link")].find((b) => b.textContent === name).click();
      return until(() => {
        const shown = $(".file-view code")?.textContent === name && $('[data-field="file-content"]');
        return shown || null;
      }, `the content of ${name}`);
    };
    let content = await open("tests/test_ok.py");
    step("a file opens as its exact text", content.textContent === "def test_ok():\n    pass\n", content.textContent);
    content = await open("notes.md");
    step("hostile file content is shown as text", content.textContent.includes('<img src=x onerror=')
      && content.textContent.includes("<script>"));
    step("and none of it became markup", !$(".file-view").querySelector("img, script, a, h1") && document.title !== "pwned");

    // -- B5: settings -----------------------------------------------------------
    const nav = (label) => [...document.querySelectorAll(".screens button")].find((b) => b.textContent.startsWith(label));
    const choose = (select, value) => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").set.call(select, value);
      select.dispatchEvent(new Event("change", { bubbles: true }));
    };
    // click a Models button and wait for that save to finish (not a previous one)
    const models = () => $('section[aria-label="Models"]');
    const saveWith = async (label) => {
      const before = Number(models().dataset.saves);
      [...models().querySelectorAll("button")].find((b) => b.textContent === label).click();
      await until(() => Number(models()?.dataset.saves) > before, `the "${label}" result`);
      return models().querySelector(".ok, .error");
    };
    nav("Settings").click();
    const warning = await until(() => $(".warning"), "the ignored-settings warning");
    step("the warning names the ignored file", /settings\.local\.yaml/.test(warning.textContent) && /ignored/.test(warning.textContent));
    // (runs 1-3 above already succeeded with this corrupt file present)

    const coder = await until(() => $('select[name="pin-coder"]'), "the coder's model picker");
    const values = [...coder.options].map((o) => o.value);
    step("the development menu build offers the mock model", values.includes("mock/mock-echo"));
    const pick = values.find((v) => v.startsWith("groq/"));
    step("real models are offered", Boolean(pick), values.length);
    choose(coder, pick);
    const result = await saveWith("Save model settings");
    step("saving model settings succeeds", result.classList.contains("ok"), result.textContent);
    step("the ignored-settings warning is gone after saving", !$(".warning") && !$(".screens .badge"));
    const chain = () => $('tr[data-agent="coder"] [data-field="chain"]').textContent;
    step("the next run's chain starts with the pinned model", chain().startsWith(pick), chain());

    const order = () => [...document.querySelectorAll(".provider-order li")].map((li) => li.dataset.provider);
    const before = order();
    $(`.provider-order li[data-provider="${before[1]}"] button[aria-label^="Move"][aria-label$="up"]`).click();
    await until(() => order()[0] === before[1], "the reorder").catch(() => {});
    step("↑ moves a provider up", order()[0] === before[1] && order()[1] === before[0], order());
    await saveWith("Save model settings");
    step("the saved order comes back from the API", order()[0] === before[1], order());

    await saveWith("Reset to defaults");
    step("reset returns every agent to its configured default",
      [...document.querySelectorAll('select[name^="pin-"]')].every((s) => s.value === ""));

    const FAKE_KEY = "AIza-smoke-fake-key-0123456789";
    const keyInput = $('input[name="key-GEMINI_API_KEY"]');
    step("key fields are password fields", keyInput.type === "password");
    type(keyInput, FAKE_KEY);
    const keysForm = () => $('form[aria-label="API keys"]');
    const keySaves = Number(keysForm().dataset.saves);
    [...keysForm().querySelectorAll("button")].find((b) => b.textContent === "Save keys").click();
    await until(() => Number(keysForm().dataset.saves) > keySaves, "the key save result");
    step("the key is saved and shown only as set",
      $('[data-key="GEMINI_API_KEY"] .key-status').textContent === "set", $('form[aria-label="API keys"] p.ok, form[aria-label="API keys"] p.error')?.textContent);
    step("the key is gone from the page",
      !document.body.innerText.includes(FAKE_KEY)
      && [...document.querySelectorAll("input")].every((i) => i.value !== FAKE_KEY));

    // -- B6: run history, cancel from the dialog, window title ---------------------
    nav("Runs").click();
    const items = await until(() => {
      const found = [...document.querySelectorAll(".run-item")];
      return found.length >= 3 ? found : null;
    }, "the runs list");
    const statuses = items.map((b) => b.querySelector(".status-pill").dataset.status);
    step("history lists every run newest first", JSON.stringify(statuses.slice(0, 3)) === '["failed","cancelled","succeeded"]', statuses);
    step("the current run is highlighted", items[0].getAttribute("aria-current") === "true");

    items[2].click(); // the first run of this session
    await until(() => $(".run h2")?.textContent === "smoke: a tiny calculator", "the old run to open");
    await until(() => pill() === "succeeded", "its status");
    await until(() => [...document.querySelectorAll(".events tr")].at(-1)?.dataset.kind === "api.run_succeeded", "its replayed log");
    step("an earlier run reopens with its replayed log",
      document.querySelectorAll(".events tr").length > 5 && $(".run-item[aria-current]")?.dataset.run === items[2].dataset.run);

    // the Settings round-trip remounted the form: pick the offline mock again
    setChecked(await until(() => $(".dev-menu input"), "the dev menu"), true);
    step("offline mock re-selected before starting", $(".dev-menu input").checked);
    type($("textarea[name=request]"), "smoke: cancel from the dialog");
    setChecked(checkbox("gate-plan"), true);
    button("Start run").click();
    d = await waitDialog("plan");
    step("the window title flags the waiting approval", /Approval needed \(plan\)/.test(document.title), document.title);
    [...d.querySelectorAll("button")].find((b) => b.textContent.trim() === "Cancel run").click();
    await until(() => pill() === "cancelled", "the cancel from the dialog");
    step("Cancel run inside the dialog cancels the run", !dialog() && pill() === "cancelled");
    step("the title is back to normal", document.title === "Senior Developer Agents", document.title);
    await until(() => $(".run-item")?.querySelector(".status-pill")?.dataset.status === "cancelled", "the list to update");
    step("the list shows the new run's final status", $(".run-item").querySelector(".status-pill").dataset.status === "cancelled");
  } catch (err) {
    report.error = err.message;
    // what the screen looked like when it failed
    report.page = {
      status: pill(),
      notice: $(".notice")?.textContent,
      dialogs: [...document.querySelectorAll("dialog")].map((d) => ({ open: d.open, title: d.querySelector("h2")?.textContent })),
      lastEvents: [...document.querySelectorAll(".events tr")].slice(-6).map((r) => r.dataset.kind),
      visibility: document.visibilityState,
    };
  }
  report.ok = !report.error && report.steps.every((s) => s.ok);
  return report;
})();
