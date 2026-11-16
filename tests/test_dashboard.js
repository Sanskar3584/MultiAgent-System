// Run with: node tests/test_dashboard.js
// Exercise the actual inline dashboard script without a browser or live API.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const html = fs.readFileSync(path.join(__dirname, "../web/dashboard.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  .split("// ---------------------------------------------------------------- start up")[0];

function dashboard(fetchImpl = async () => { throw new Error("No network in this test"); }) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      hidden: false, textContent: "", innerHTML: "", className: "", value: "",
      addEventListener() {}, classList: { toggle() {} }, focus() {},
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    document: { getElementById: element, querySelector: element, querySelectorAll: () => [] },
    location: { hash: "" }, fetch: fetchImpl, AbortSignal, Date,
    setInterval: () => 1, clearInterval() {}, setTimeout: () => 1, console,
  });
  vm.runInContext(script + `
    globalThis.harness = {
      handle, draw, poll, taskOf, reconcileRun, columnsOf, markdown,
      loadRoutines, routineResults,
      begin(id = "run-test") {
        generation++;
        currentRun = id;
        seen = 0;
        view = emptyView();
      },
      state() { return { view, seen }; },
    };
  `, context);
  context.harness.begin();
  return { ...context.harness, element };
}

function response(data) {
  return { ok: true, json: async () => data };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

const plan = [
  { id: "research", role: "researcher", title: "Research", depends_on: [] },
  { id: "draft", role: "writer", title: "Draft", depends_on: ["research"] },
];

function researchStarted(app) {
  app.handle({ kind: "run_status", who: "orchestrator", status: "working", goal: "Explain renewable energy", plan });
  app.handle({ kind: "task_status", who: "researcher:research", task: "research", status: "running" });
  app.handle({ kind: "check", who: "researcher:research", attempt: 2, check: "citations", passed: true, problems: [] });
}

test("a terminal failure reveals its reason and ends running/waiting cards", () => {
  const app = dashboard();
  researchStarted(app);
  app.handle({ kind: "run_status", who: "orchestrator", status: "failed", reason: "Groq rejected an unknown tool: json" });
  app.draw();
  assert.equal(app.state().view.taskStatus.research, "failed");
  assert.equal(app.state().view.taskStatus.draft, "skipped");
  assert.equal(app.element("run-error").hidden, false);
  assert.match(app.element("run-error").textContent, /unknown tool: json/);
  assert.match(app.element("feed").innerHTML, /unknown tool: json/);
  assert.doesNotMatch(app.element("pipeline").innerHTML, /badge running/);
});

test("failed final output is displayed as failed, not done", () => {
  const app = dashboard();
  researchStarted(app);
  app.handle({ kind: "final", who: "combiner", output: "An unverified answer", passed: false });
  app.handle({ kind: "run_status", who: "orchestrator", status: "partial" });
  app.draw();
  assert.match(app.element("pipeline").innerHTML, /Combine the results<\/h3><span class="badge failed">failed/);
  assert.match(app.element("result").innerHTML, /An unverified answer/);
});

test("a task failure reason remains visible when the run event has no reason", () => {
  const app = dashboard();
  researchStarted(app);
  app.handle({ kind: "task_status", who: "researcher:research", task: "research", status: "failed", reason: "Review request timed out" });
  app.handle({ kind: "run_status", who: "orchestrator", status: "failed" });
  app.draw();
  assert.match(app.element("run-error").textContent, /Research: Review request timed out/);
  assert.match(app.element("pipeline").innerHTML, /Review request timed out/);
});

test("task errors in a report survive a missing event stream", () => {
  const app = dashboard();
  app.reconcileRun({ status: "failed", report: {
    plan, tasks: { research: { status: "failed", error: "Groq rejected the review" }, draft: { status: "skipped" } },
    final_output: "", final_passed: false,
  } });
  app.draw();
  assert.match(app.element("run-error").textContent, /Research: Groq rejected the review/);
  assert.match(app.element("pipeline").innerHTML, /Groq rejected the review/);
});

test("reviewer model activity appears on the corresponding task", () => {
  const app = dashboard();
  researchStarted(app);
  app.handle({ kind: "model_status", who: "reviewer:research", status: "waiting", time: Date.now() / 1000 - 4 });
  app.draw();
  assert.match(app.element("pipeline").innerHTML, /Waiting for reviewer \(4s\)/);
  assert.equal(app.taskOf("reviewer:final"), "combiner");
  assert.equal(app.taskOf("reviewer:plan"), "planner");
  app.handle({ kind: "model_status", who: "reviewer:research", status: "done", seconds: 4 });
  app.draw();
  assert.doesNotMatch(app.element("pipeline").innerHTML, /Waiting for reviewer/);
});

test("overlapping polling cannot duplicate events or advance the cursor twice", async () => {
  const events = deferred();
  const run = deferred();
  let requests = 0;
  const app = dashboard((url) => {
    requests++;
    return url.includes("/events?") ? events.promise : run.promise;
  });
  const first = app.poll();
  const second = app.poll();
  assert.equal(requests, 2); // one event request and one status request
  events.resolve(response({ events: [{ kind: "run_status", who: "orchestrator", status: "working" }], total: 7 }));
  run.resolve(response({ status: "running" }));
  await Promise.all([first, second]);
  assert.equal(app.state().seen, 7);
  assert.equal(app.state().view.feed.length, 1);
});

test("a response from a previously opened view cannot change the current run", async () => {
  const oldResponse = deferred();
  const app = dashboard(() => oldResponse.promise);
  const oldPoll = app.poll();
  // Reopening the same ID must also invalidate the previous view's response.
  app.begin("run-test");
  oldResponse.resolve(response({ events: [{ kind: "run_status", who: "orchestrator", status: "failed", reason: "old failure" }], total: 9, status: "failed" }));
  await oldPoll;
  assert.equal(app.state().seen, 0);
  assert.equal(app.state().view.status, "planning");
  assert.equal(app.state().view.error, "");
});

test("the run snapshot recovers a terminal error missing from the event stream", async () => {
  const app = dashboard(async (url) => response(url.includes("/events?")
    ? { events: [], total: 3 }
    : { status: "error", error: "Groq could not finish the review" }));
  researchStarted(app);
  await app.poll();
  assert.equal(app.state().view.status, "failed");
  assert.equal(app.state().view.taskStatus.research, "failed");
  assert.equal(app.state().view.taskStatus.draft, "skipped");
  assert.match(app.element("run-error").textContent, /could not finish the review/);
});

test("connection failures are visible and disappear after recovery", async () => {
  let connected = false;
  const app = dashboard(async (url) => {
    if (!connected) throw new Error("Connection lost");
    return response(url.includes("/events?") ? { events: [], total: 0 } : { status: "running" });
  });
  await app.poll();
  assert.equal(app.element("run-connection").hidden, false);
  assert.match(app.element("run-connection").textContent, /Cannot reach this run/);
  connected = true;
  await app.poll();
  assert.equal(app.element("run-connection").hidden, true);
});

test("task columns follow dependencies even when the plan arrives out of order", () => {
  const app = dashboard();
  const columns = app.columnsOf([
    { id: "docs", depends_on: ["code", "research"] },
    { id: "code", depends_on: ["research"] },
    { id: "research", depends_on: [] },
    { id: "independent", depends_on: [] },
  ]);
  assert.equal(columns.research, 1);
  assert.equal(columns.independent, 1);
  assert.equal(columns.code, 2);
  assert.equal(columns.docs, 3);
});

test("result formatting keeps markdown features and escapes model-provided HTML", () => {
  const app = dashboard();
  const result = app.markdown([
    "# Result", "## Details", "### Example", "- First", "- Second",
    "**Important** and `inline()`", "<script>alert('bad')</script>",
    "```python", "# code heading", "print('<tag>')", "**not bold**", "```",
  ].join("\n"));
  assert.match(result, /<h2>Result<\/h2>/);
  assert.match(result, /<h3>Details<\/h3>/);
  assert.match(result, /<h3>Example<\/h3>/);
  assert.match(result, /<ul><li>First<\/li>\n<li>Second<\/li>/);
  assert.match(result, /<strong>Important<\/strong> and <code>inline\(\)<\/code>/);
  assert.match(result, /&lt;script&gt;alert\(&#39;bad&#39;\)&lt;\/script&gt;/);
  assert.match(result, /<pre><code># code heading\nprint\(&#39;&lt;tag&gt;&#39;\)\n\*\*not bold\*\*\n<\/code><\/pre>/);
  assert.doesNotMatch(result, /<script>|<tag>|<strong>not bold<\/strong>/);
});

test("routine tab preserves schedules, run buttons, and step result stamps", async () => {
  const app = dashboard(async () => response([
    { name: "audit", description: "Check <documents>", schedule: { every_minutes: 30 }, next_run: null },
    { name: "manual", description: "Manual report", schedule: null, next_run: null },
  ]));
  app.routineResults.audit = { status: "running", steps: [
    { id: "read", status: "ok" },
    { id: "check", status: "failed", error: 'Missing "source"' },
    { id: "post", status: "skipped" },
  ] };
  await app.loadRoutines();
  const rendered = app.element("tab-routines").innerHTML;
  assert.match(rendered, /every 30 minutes/);
  assert.match(rendered, /manual only/);
  assert.match(rendered, /Check &lt;documents&gt;/);
  assert.match(rendered, /stamp pass[^>]*>read/);
  assert.match(rendered, /stamp fail[^>]*title="Missing &quot;source&quot;">check/);
  assert.match(rendered, /stamp wait[^>]*>post/);
  assert.match(rendered, /data-routine="audit">Running…/);
  assert.match(rendered, /data-routine="manual">Run now/);
});
