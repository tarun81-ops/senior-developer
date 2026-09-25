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
  const pill = () => $(".status-pill")?.dataset.status;

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
  } catch (err) {
    report.error = err.message;
  }
  report.ok = !report.error && report.steps.every((s) => s.ok);
  return report;
})();
