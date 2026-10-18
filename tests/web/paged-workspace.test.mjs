import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { JSDOM, VirtualConsole } from "jsdom";

const asset = (name) => readFileSync(new URL(`../../src/auto_zettelkasten/web/${name}`, import.meta.url), "utf8");
const tick = () => new Promise((resolve) => setTimeout(resolve, 5));
async function until(check, label) {
  const deadline = Date.now() + 10000;
  while (!check()) { assert.ok(Date.now() < deadline, label); await tick(); }
}
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };

function harness(t, index, read) {
  const errors = [], requests = [], graphs = [], polls = [];
  const console = new VirtualConsole(); console.on("jsdomError", (error) => errors.push(error));
  const dom = new JSDOM(asset("index.html"), { url: "http://127.0.0.1:8765", runScripts: "dangerously", pretendToBeVisual: true, virtualConsole: console });
  const w = dom.window;
  w.HTMLElement.prototype.scrollIntoView = function () {};
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; };
  w.Range.prototype.getClientRects = () => [];
  w.Range.prototype.getBoundingClientRect = () => ({ left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 });
  w.setInterval = (callback) => { polls.push(callback); return polls.length; };
  const workspace = { id: "root", path: "/notes", name: "Notes", editable: true, truncated: true, entries: [{ path: "Start.md", name: "Start.md", kind: "file", editable: true }] };
  w.fetch = async (url, request) => {
    const body = JSON.parse(request?.body || "{}"); requests.push({ url, body });
    let result, status = 200;
    if (url.endsWith("/open") || url.endsWith("/tree")) result = { workspace: { ...workspace, id: body.path || workspace.id } };
    else if (url.endsWith("/index")) { result = await index(body); if (result.code) status = 409; }
    else if (url.endsWith("/read")) {
      const value = await read?.(body);
      if (value?.error) { result = value; status = 500; }
      else result = { document: value || { id: body.path, path: body.path, name: body.path, text: "# Open note\n[[Start]]", editable: true, revision: "r", reason: "", recovery_key: body.path } };
    }
    else throw new Error(`Unexpected request ${url}`);
    return { ok: status === 200, status, json: async () => structuredClone(result) };
  };
  const load = (name) => { const script = w.document.createElement("script"); script.textContent = asset(name); w.document.body.append(script); };
  load("workspace-vendor.js");
  const kit = w.WorkspaceKit;
  w.WorkspaceKit = { ...kit,
    // RPC/yield behavior is separately tested; keep this test focused on staging.
    LibraryParser: class { async parse(documents) { return documents.map(kit.parseLibraryDocument); } close() {} },
    cytoscape: (options) => { const config = w.Object.assign(new w.Object(), options); config.container = undefined; config.headless = true; const graph = kit.cytoscape(config); graphs.push(graph); return graph; },
    layoutGraph: () => ({ finished: Promise.resolve({ status: "complete" }), cancel() {} }),
  };
  load("app.js"); load("workspace.js");
  const el = (id) => w.document.getElementById(id), click = (id) => el(id).click();
  const input = (id, value) => { el(id).value = value; el(id).dispatchEvent(new w.Event("input", { bubbles: true })); };
  const root = async (path = "/notes") => {
    click(workspace.id === "root" && !requests.length ? "workspace-welcome-open" : "workspace-root"); input("workspace-folder-path", path);
    el("workspace-folder-form").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
    await until(() => requests.some((request) => request.url.endsWith("/index") && request.body.workspace_id === path), "index started");
  };
  const open = async (path) => {
    click("workspace-quick-open"); input("workspace-quick-query", path);
    el("workspace-quick-query").dispatchEvent(new w.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await until(() => el("workspace-breadcrumbs").title === path, `opened ${path}`);
  };
  t.after(() => { for (const graph of graphs) graph.destroy(); w.close(); assert.deepEqual(errors, []); });
  return { w, el, click, input, root, open, requests, graphs, poll: () => polls[0]() };
}

test("paged library commits every note and relation only after verification, then reuses its generation", async (t) => {
  const last = deferred();
  const count = 6001;
  let complete = false;
  const app = harness(t, async (body) => {
    assert.equal(body.paged, true);
    if (body.known_generation || body.cursor === "check") return { generation: "g1", documents: [], entries: [], relations: [], complete: body.cursor === "check", not_modified: body.cursor === "check", next_cursor: "check" };
    const start = Number(body.cursor || 0), end = Math.min(count, start + 32);
    if (end === count) { await last.promise; complete = true; }
    const documents = Array.from({ length: end - start }, (_, index) => ({ path: `N${start + index}.md`, text: `# N${start + index}\n[[N${(start + index + 1) % count}]]`, revision: "r", editable: false }));
    return { generation: "g1", documents, entries: documents.map((doc) => ({ path: doc.path, name: doc.path, kind: "file", editable: false })), relations: end === count ? [{ id: "relation", source: "N0.md", target: "N6000.md", type: "supports" }] : [], warnings: [], truncated: false, complete: end === count, next_cursor: String(end), progress: { documents: end } };
  });
  await app.root();
  await until(() => app.requests.some((request) => request.body.cursor === "5984"), "last page pending");
  app.click("workspace-graph-open");
  assert.match(app.el("global-graph-counts").textContent, /^0 notes/);
  assert.match(app.el("workspace-index-status").textContent, /Indexing/);
  last.resolve();
  await until(() => /^6001 notes/.test(app.el("global-graph-counts").textContent), "all notes committed");
  assert.equal(complete, true);
  assert.equal(app.graphs.at(-1).nodes().length, count);
  assert.equal(app.graphs.at(-1).edges().length, count + 1);
  assert.doesNotMatch(app.el("global-graph-counts").textContent, /partial/);
  assert.equal(app.el("workspace-tree-note").hidden, true);
  assert.equal(app.el("global-graph-list").children.length, 0, "large hidden list is lazy");
  app.click("global-graph-show-list");
  assert.equal(app.el("global-graph-list").children.length, 201, "keyboard list is paged without dropping graph nodes");
  app.click("workspace-quick-open"); app.input("workspace-quick-query", "N6000");
  const result = app.el("workspace-quick-results").querySelector("button"); assert.ok(result);
  await app.poll();
  assert.equal(app.el("workspace-quick-results").querySelector("button"), result);
  assert.ok(app.requests.some((request) => request.body.known_generation === "g1"));
  assert.equal(app.requests.filter((request) => request.url.endsWith("/tree")).length, 0);
  result.click();
  await until(() => app.el("workspace-breadcrumbs").title === "N6000.md", "note beyond previous document limit opens");
});

test("changed generations retry once and stale pages cannot replace a newly opened workspace", async (t) => {
  const stale = deferred(); let oldRequests = 0;
  const app = harness(t, async (body) => {
    if (body.workspace_id === "/old") {
      oldRequests += 1;
      if (oldRequests === 1) return { error: "Changed", code: "index_changed" };
      await stale.promise;
    }
    const path = body.workspace_id === "/old" ? "Old.md" : "New.md";
    return { generation: path, complete: true, documents: [{ path, text: "# Note", revision: "r" }], entries: [{ path, name: path, kind: "file" }], relations: [], warnings: [], truncated: false };
  });
  await app.root("/old"); await until(() => oldRequests === 2, "one retry");
  await app.root("/new");
  await until(() => app.el("workspace-tree").textContent.includes("New"), "new root committed");
  stale.resolve(); await tick(); await tick();
  assert.doesNotMatch(app.el("workspace-tree").textContent, /Old/);
  assert.equal(oldRequests, 2);
  assert.doesNotMatch(app.el("workspace-index-status").textContent, /could not refresh/);
});

const note = (path, text, revision = "r") => ({ id: path, path, name: path, text, revision, editable: false, reason: "Generated", recovery_key: path });
const page = (generation, notes) => ({ generation, complete: true, documents: [...notes.values()], entries: [...notes.values()].map((doc) => ({ path: doc.path, name: doc.path, kind: "file", editable: doc.editable })), relations: [], warnings: [], truncated: false });

test("a failed inactive-note reread retains the previous generation and retries before claiming unchanged", async (t) => {
  const notes = new Map([note("Start.md", "# Start"), note("B.md", "# B\nOld body", "b1")].map((value) => [value.path, value]));
  let generation = "g1", fail = false, changedReads = 0;
  const app = harness(t, async (body) => body.known_generation === generation ? { generation, complete: true, not_modified: true } : page(generation, notes), async ({ path }) => {
    if (path === "B.md" && generation === "g2") { changedReads += 1; if (fail) { fail = false; return { error: "Temporary read failure" }; } }
    return { ...notes.get(path) };
  });
  await app.root(); await until(() => app.el("workspace-tree-note").hidden, "initial complete index");
  await app.open("B.md"); await app.open("Start.md");
  generation = "g2"; fail = true; notes.set("B.md", note("B.md", "# B\nNew body", "b2"));
  await app.poll();
  assert.match(app.el("workspace-index-status").textContent, /Temporary read failure/);
  await app.poll();
  assert.equal(changedReads, 2, "inactive note read is retried");
  const checks = app.requests.filter((request) => request.url.endsWith("/index"));
  assert.equal(checks.at(-1).body.known_generation, "g1", "failed reconciliation never commits g2");
  app.el("workspace-tabs").querySelector('[role="tab"][title="B.md"]').click();
  assert.match(app.el("workspace-reader").textContent, /New body/);
  assert.equal(app.el("workspace-index-status").hidden, true);
});

test("interrupted embed hydration retries on return and rejects a stale generation reply", async (t) => {
  const notes = new Map([note("Start.md", "# Start\n![[Target]]"), note("B.md", "# B"), note("Target.md", "# Target\nOld embedded body", "t1")].map((value) => [value.path, value]));
  let generation = "g1", embeddedReads = 0;
  const first = deferred(), second = deferred();
  const app = harness(t, async (body) => body.known_generation === generation ? { generation, complete: true, not_modified: true } : page(generation, notes), async ({ path }) => {
    if (path === "Target.md") {
      embeddedReads += 1;
      if (embeddedReads === 1) { await first.promise; return note(path, "# Target\nOld embedded body", "t1"); }
      if (embeddedReads === 2) { await second.promise; return note(path, "# Target\nStale embedded body", "t1"); }
    }
    return { ...notes.get(path) };
  });
  await app.root(); await until(() => app.el("workspace-tree-note").hidden, "initial complete index");
  await app.open("Start.md"); await until(() => embeddedReads === 1, "first embed read started");
  await app.open("B.md"); first.resolve(); await tick(); await tick();
  await app.open("Start.md"); await until(() => embeddedReads === 2, "interrupted read can retry");
  generation = "g2"; notes.set("Target.md", note("Target.md", "# Target\nNew embedded body", "t2"));
  await app.poll();
  await until(() => app.el("workspace-reader").textContent.includes("New embedded body"), "new generation embed hydrated");
  second.resolve(); await tick(); await tick();
  assert.doesNotMatch(app.el("workspace-reader").textContent, /Stale embedded body/);
  assert.match(app.el("workspace-reader").textContent, /New embedded body/);
});
