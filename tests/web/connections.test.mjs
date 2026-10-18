import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { JSDOM, VirtualConsole } from "jsdom";

const asset = (name) => readFileSync(new URL(`../../src/auto_zettelkasten/web/${name}`, import.meta.url), "utf8");
async function until(check, label) {
  for (let tries = 0; tries < 400; tries += 1) {
    if (check()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.fail(`Timed out: ${label}`);
}

function harness(t, texts, relations = []) {
  const errors = [], virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (error) => errors.push(error));
  const dom = new JSDOM(asset("index.html"), { url: "http://127.0.0.1:8765", runScripts: "dangerously", pretendToBeVisual: true, virtualConsole });
  const w = dom.window;
  w.HTMLElement.prototype.scrollIntoView = function () { this.dataset.scrolled = "true"; };
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; };
  w.Range.prototype.getClientRects = () => [];
  w.Range.prototype.getBoundingClientRect = () => ({ left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 });
  const notes = new Map(), requests = [], hooks = {}, graphs = [], graphOptions = [], polls = [];
  let revision = 0;
  const put = (path, text) => notes.set(path, { id: path, path, name: path.split("/").at(-1), text, revision: String(++revision), editable: true, reason: "", bom: false, newline: text.includes("\r\n") ? "crlf" : "lf", recovery_key: path });
  for (const [path, text] of Object.entries(texts)) put(path, text);
  const workspace = () => ({ id: "root", name: "Notes", path: "/notes", editable: true, truncated: false, entries: [...notes.keys()].map((path) => ({ path, name: path.split("/").at(-1), kind: "file", editable: true })) });
  const interval = w.setInterval.bind(w);
  w.setInterval = (callback, delay) => { polls.push(callback); return interval(callback, delay); };
  w.fetch = async (url, request) => {
    const body = JSON.parse(request.body || "{}"); requests.push({ url, body });
    let result;
    if (url.endsWith("/open") || url.endsWith("/tree")) result = { workspace: workspace() };
    else if (url.endsWith("/index")) { result = { documents: [...notes.values()], relations, warnings: [], truncated: false }; await hooks.index?.(result); }
    else if (url.endsWith("/read")) { result = { document: { ...notes.get(body.path) } }; await hooks.read?.(body); }
    else if (url.endsWith("/save")) { put(body.document_id, body.text); result = { document: notes.get(body.document_id) }; }
    else throw new Error(`Unexpected request ${url}`);
    return { ok: true, status: 200, json: async () => structuredClone(result) };
  };
  const load = (name) => { const script = w.document.createElement("script"); script.textContent = asset(name); w.document.body.append(script); };
  load("workspace-vendor.js");
  const kit = w.WorkspaceKit;
  // Canvas behavior is exercised in the real browser; these tests use real headless graph data.
  w.WorkspaceKit = { ...kit, layoutGraph: (graph, options) => hooks.layout?.(graph, options) ?? kit.layoutGraph(graph, options), cytoscape: (options) => { graphOptions.push(options); const local = w.Object.assign(new w.Object(), options); local.container = undefined; local.headless = true; local.styleEnabled = true; const graph = kit.cytoscape(local); graphs.push(graph); return graph; } };
  load("app.js"); load("workspace.js");
  const el = (id) => w.document.getElementById(id), click = (id) => el(id).click();
  const view = () => kit.EditorView.findFromDOM(w.document.querySelector(".cm-editor"));
  const input = (id, value) => { el(id).value = value; el(id).dispatchEvent(new w.Event("input", { bubbles: true })); };
  const open = async (path) => {
    click("workspace-quick-open"); input("workspace-quick-query", path);
    el("workspace-quick-query").dispatchEvent(new w.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await until(() => el("workspace-breadcrumbs").title === path, `open ${path}`);
    return view();
  };
  const root = async (path = "Start.md") => {
    click("workspace-welcome-open"); input("workspace-folder-path", "/notes");
    el("workspace-folder-form").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
    await until(() => requests.some((request) => request.url.endsWith("/index")), "index request");
    await until(() => !/^(Checking|Indexing|Verifying)/.test(el("workspace-index-status").textContent), "background index complete");
    await open(path);
  };
  const mode = (mode) => w.document.querySelector(`[data-mode="${mode}"]`).click();
  const change = (id, value) => { el(id).checked = value; el(id).dispatchEvent(new w.Event("change", { bubbles: true })); };
  t.after(() => { for (const graph of graphs) if (graph && !graph.destroyed()) graph.destroy(); w.close(); assert.deepEqual(errors, []); });
  return { w, el, click, input, view, notes, put, requests, hooks, graphs, graphOptions, open, root, mode, change, poll: () => polls[0]() };
}

const texts = {
  "Start.md": "# Start\n\n[[Target|Target label]]\n\n[Standard target](Notes/Target.md#Section)\n",
  "Editor trial.md": "# Editor trial\n\n[[Target alias]]\n[[Target#Section]]\n[[Target#^test-block]]\n![[Target#Section]]\n[[Target#^second-block]]\n",
  "Notes/Target.md": '---\naliases: [Target alias]\ntags: [ideas]\n---\n# Target\n\n## Section\n\nTarget paragraph. ^test-block\n\nSecond paragraph. ^second-block\n\n## Following\n\nOutside section.\n',
  "Orphan.md": "# Orphan\n",
};

test("Tab targets stay attached across edits and refreshes, including a pressed close button", async (t) => {
  const app = harness(t, { "Start.md": "# Start", "B.md": "# B" });
  await app.root(); await app.open("B.md"); await app.open("Start.md");
  const tabs = app.el("workspace-tabs");
  const start = tabs.querySelector('[role="tab"][title="Start.md"]');
  const close = tabs.querySelector('[aria-label="Close B.md"]');
  close.dispatchEvent(new app.w.MouseEvent("pointerdown", { bubbles: true, button: 0 }));
  app.view().dispatch({ changes: { from: app.view().state.doc.length, insert: "\nDraft" } });
  assert.equal(tabs.querySelector('[role="tab"][title="Start.md"]'), start);
  assert.equal(tabs.querySelector('[aria-label="Close B.md"]'), close);
  await app.poll();
  assert.equal(tabs.querySelector('[aria-label="Close B.md"]'), close);
  close.dispatchEvent(new app.w.MouseEvent("pointerup", { bubbles: true, button: 0 }));
  close.click();
  assert.equal(tabs.querySelector('[aria-label="Close B.md"]'), null);
  assert.equal(app.el("workspace-breadcrumbs").title, "Start.md");
  assert.match(app.view().state.doc.toString(), /Draft$/);
});

test("Switching existing tabs is immediate and graph participates in tab keyboard navigation", async (t) => {
  const app = harness(t, { "Start.md": "# Start", "B.md": "# B" });
  await app.root(); await app.open("B.md");
  const tabs = app.el("workspace-tabs");
  const reads = app.requests.filter((request) => request.url.endsWith("/read")).length;
  tabs.querySelector('[role="tab"][title="Start.md"]').click();
  assert.equal(app.el("workspace-breadcrumbs").title, "Start.md");
  assert.equal(app.requests.filter((request) => request.url.endsWith("/read")).length, reads);
  app.click("workspace-graph-open");
  const graph = [...tabs.querySelectorAll('[role="tab"]')].find((tab) => tab.textContent === "Graph view");
  graph.dispatchEvent(new app.w.KeyboardEvent("keydown", { key: "Home", bubbles: true }));
  assert.equal(app.el("workspace-breadcrumbs").title, "Start.md");
  assert.equal(app.el("workspace-global-graph").hidden, true);
  tabs.querySelector('[role="tab"][title="Start.md"]').dispatchEvent(new app.w.KeyboardEvent("keydown", { key: "End", bubbles: true }));
  assert.equal(app.el("workspace-global-graph").hidden, false);
  delete graph.parentElement.dataset.scrolled;
  app.w.dispatchEvent(new app.w.Event("resize"));
  await until(() => graph.parentElement.dataset.scrolled === "true", "active tab stays visible after resize");
});

test("Closing the last note selects its remaining graph tab and dirty-close cancellation preserves its target", async (t) => {
  const app = harness(t, { "Start.md": "# Start" }); await app.root();
  app.click("workspace-graph-open"); await app.open("Start.md");
  const close = app.el("workspace-tabs").querySelector('[aria-label="Close Start.md"]');
  app.view().dispatch({ changes: { from: 0, insert: "Draft\n" } });
  close.click();
  await until(() => app.el("workspace-confirm-dialog").open, "dirty close review");
  app.click("workspace-confirm-cancel"); await Promise.resolve();
  assert.equal(app.el("workspace-tabs").querySelector('[aria-label="Close Start.md"]'), close);
  assert.match(app.view().state.doc.toString(), /^Draft/);
  close.click(); await until(() => app.el("workspace-confirm-dialog").open, "second close review");
  app.click("workspace-confirm-accept");
  await until(() => !app.el("workspace-tabs").querySelector('[aria-label="Close Start.md"]'), "close last note");
  assert.equal(app.el("workspace-global-graph").hidden, false);
});

test("Quick open dismisses on an outside click, retains inside drags, and restores focus", async (t) => {
  const app = harness(t, texts); await app.root();
  const dialog = app.el("workspace-quick-dialog"), trigger = app.el("workspace-quick-open");
  dialog.getBoundingClientRect = () => ({ left: 100, right: 500, top: 100, bottom: 400 });
  const pointer = (target, type, x, y) => target.dispatchEvent(new app.w.MouseEvent(type, { bubbles: true, button: 0, clientX: x, clientY: y }));
  trigger.focus(); trigger.click();
  pointer(dialog, "pointerdown", 120, 110); pointer(dialog, "click", 120, 110);
  assert.equal(dialog.open, true, "dialog padding is not the backdrop");
  pointer(app.el("workspace-quick-query"), "pointerdown", 150, 120); pointer(dialog, "click", 10, 10);
  assert.equal(dialog.open, true, "text selection dragging outside stays open");
  pointer(dialog, "pointerdown", 10, 10); pointer(dialog, "click", 10, 10);
  assert.equal(dialog.open, false);
  assert.equal(app.w.document.activeElement, trigger);
  trigger.click(); dialog.dispatchEvent(new app.w.Event("cancel", { cancelable: true }));
  assert.equal(dialog.open, false);
  assert.equal(app.w.document.activeElement, trigger);
  trigger.click(); dialog.dispatchEvent(new app.w.Event("close"));
  assert.equal(dialog.open, true, "a queued close event cannot dismiss a newly reopened picker");
});

test("Unchanged index refresh keeps quick-open result click targets", async (t) => {
  const app = harness(t, texts); await app.root();
  app.click("workspace-quick-open"); app.input("workspace-quick-query", "Target alias");
  const result = app.el("workspace-quick-results").querySelector("button");
  await app.poll();
  assert.equal(app.el("workspace-quick-results").querySelector("button"), result);
  result.click();
  await until(() => app.el("workspace-breadcrumbs").title === "Notes/Target.md", "open retained result");
  assert.equal(app.el("workspace-quick-dialog").open, false);
});

test("Reading follows shared links; backlinks show seven occurrences grouped into two source notes", async (t) => {
  const app = harness(t, texts); await app.root(); app.mode("reading");
  const link = app.el("workspace-reader").querySelector('[data-note-destination="Target"]'); assert.ok(link);
  link.click(); await until(() => app.el("workspace-breadcrumbs").title === "Notes/Target.md", "target link");
  app.click("workspace-backlinks-toggle");
  assert.equal(app.el("workspace-backlinks-title").textContent, "Linked mentions · 7");
  assert.equal(app.el("workspace-backlink-items").querySelectorAll("details").length, 2);
  assert.equal(app.el("workspace-backlink-items").querySelectorAll("button").length, 7);
  app.el("workspace-backlink-items").querySelector("button").click();
  await until(() => app.el("workspace-breadcrumbs").title === "Editor trial.md", "backlink source");
  assert.match(app.view().state.sliceDoc(app.view().state.selection.main.from, app.view().state.selection.main.to), /\[\[Target alias\]\]/);
});

test("Alias quick-open and wiki completion keep one closing pair and navigate headings and existing blocks", async (t) => {
  const app = harness(t, texts); await app.root();
  app.click("workspace-quick-open"); app.input("workspace-quick-query", "Target alias");
  assert.match(app.el("workspace-quick-results").textContent, /Target alias.*Notes\/Target.md/s);
  app.el("workspace-quick-results").querySelector("button").click(); await until(() => app.el("workspace-breadcrumbs").title === "Notes/Target.md", "alias target");
  await app.open("Start.md");
  const view = app.view(), content = "# Start\n\n[[Target a]]";
  view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: content }, selection: { anchor: content.length - 2 } });
  app.w.WorkspaceKit.startCompletion(view); await until(() => app.w.WorkspaceKit.completionStatus(view.state) === "active", "wiki completion");
  assert.equal(app.w.WorkspaceKit.acceptCompletion(view), true);
  assert.equal(view.state.doc.toString().match(/\]\]/g).length, 1);
  assert.match(view.state.doc.toString(), /\[\[Target\|Target alias\]\]$/);
  const heading = "# Start\n\n[[Target#Section]]";
  view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: heading }, selection: { anchor: heading.length - 3 } });
  view.contentDOM.dispatchEvent(new app.w.KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }));
  await until(() => app.el("workspace-breadcrumbs").title === "Notes/Target.md", "heading jump");
  assert.equal(app.view().state.selection.main.head, app.notes.get("Notes/Target.md").text.indexOf("## Section"));
});

test("Global and linked local graph controls preserve orphans, expose missing notes, and follow or pin context", async (t) => {
  const app = harness(t, { "Start.md": "# Start\n\n[[B]] [[Missing]]", "B.md": "# B\n\n[[C]]", "C.md": "# C", "Orphan.md": "# Orphan" });
  await app.root(); app.click("workspace-graph-open");
  const global = app.graphs[0]; assert.deepEqual([...global.nodes().map((node) => node.id())].sort(), ["B.md", "C.md", "Orphan.md", "Start.md"]);
  app.change("global-graph-existing", false); assert.equal(global.nodes().length, 5);
  app.input("global-graph-search", "Orphan"); assert.equal(global.nodes().length, 1); assert.equal(global.nodes()[0].id(), "Orphan.md");
  app.input("global-graph-search", ""); app.click("global-graph-show-list");
  [...app.el("global-graph-list").querySelectorAll("button")].find((button) => button.textContent === "B").click();
  await until(() => app.el("workspace-breadcrumbs").title === "B.md" && app.el("workspace-global-graph").hidden, "graph navigation");
  app.click("workspace-local-toggle"); const local = app.graphs.at(-1);
  assert.deepEqual([...local.nodes().map((node) => node.id())].sort(), ["B.md", "C.md", "Start.md"]);
  app.click("local-graph-pin"); await app.open("C.md"); assert.equal(app.el("local-graph-title").textContent, "Graph of B");
  app.click("workspace-graph-open");
  assert.equal(global.getElementById("C.md").data("current"), "yes");
  assert.equal(global.getElementById("B.md").data("current"), "no");
  await app.open("C.md");
  app.click("local-graph-pin"); assert.equal(app.el("local-graph-title").textContent, "Graph of C");
  assert.equal(app.w.workspaceHasUnsavedChanges(), false);
});

test("Graph refreshes and label changes retain node positions, viewport, and neighbor highlighting", async (t) => {
  const app = harness(t, { "Start.md": "# Start\n[[B]]", "B.md": "# B", "Orphan.md": "# Orphan" });
  await app.root(); app.click("workspace-graph-open");
  const graph = app.graphs[0], node = graph.getElementById("Start.md");
  graph.emit("grab");
  const point = (x, y) => app.w.Object.assign(new app.w.Object(), { x, y });
  node.position(point(125, 200)); graph.viewport({ zoom: .9, pan: point(60, 80) });
  assert.deepEqual({ ...node.position() }, { x: 125, y: 200 });
  assert.deepEqual({ ...graph.pan() }, { x: 60, y: 80 });
  const position = { ...node.position() }, pan = { ...graph.pan() };
  app.change("global-graph-labels", false); await app.poll();
  assert.equal(graph.getElementById("Start.md")[0], node[0]);
  assert.deepEqual({ ...node.position() }, position);
  assert.deepEqual({ ...graph.pan() }, pan); assert.equal(graph.zoom(), .9);
  assert.equal(node.style("label"), "");
  node.emit("mouseover");
  assert.equal(node.style("label"), "Start"); assert.equal(node.style("text-opacity"), "1");
  assert.equal(graph.getElementById("B.md").hasClass("neighbor"), true);
  assert.equal(graph.getElementById("Orphan.md").hasClass("dimmed"), true);
  node.emit("mouseout"); assert.equal(graph.elements(".dimmed,.neighbor,.hovered").length, 0);
  app.change("global-graph-labels", true); graph.zoom(.2);
  await until(() => node.style("text-opacity") === "0", "zoomed-out labels");
  assert.equal(node.style("text-opacity"), "0");
  graph.zoom(1); await until(() => node.style("text-opacity") === "1", "zoomed-in labels");
});

test("Global and local index-note toggles filter the view while preserving notes, backlinks and direct relations", async (t) => {
  const indexPath = "02_source_memory/indexes/by_topic/topic-one.md";
  const app = harness(t, { "Start.md": "# Start\n[[B]]", "B.md": "# B", [indexPath]: "# Topic\n[[Start]] [[B]]" }, [{ id: "accepted", source: "Start.md", target: "B.md", type: "supports" }]);
  await app.root(); app.click("workspace-graph-open");
  const global = app.graphs[0], start = global.getElementById("Start.md")[0];
  assert.equal(app.el("global-graph-indexes").checked, true);
  assert.equal(global.nodes().length, 3); assert.equal(global.edges().length, 4);
  app.click("global-graph-show-list");
  app.change("global-graph-indexes", false);
  assert.deepEqual([...global.nodes().map((node) => node.id())].sort(), ["B.md", "Start.md"]);
  assert.equal(global.edges().length, 2);
  assert.equal(global.edges(".relation").length, 1);
  assert.equal(global.getElementById("Start.md")[0], start);
  assert.equal(app.el("global-graph-list").textContent.includes("topic-one"), false);
  assert.match(app.el("global-graph-counts").textContent, /^2 notes · 2 connections · indexes hidden$/);
  await app.open("B.md"); app.click("workspace-backlinks-toggle");
  assert.match(app.el("workspace-backlinks").textContent, /Linked mentions · 2/);
  app.click("workspace-local-toggle"); const local = app.graphs.at(-1);
  assert.equal(app.el("local-graph-indexes").checked, true, "Local and global view filters are independent");
  assert.equal(local.nodes().length, 3);
  app.change("local-graph-indexes", false);
  assert.equal(local.nodes().length, 2); assert.equal(local.edges().length, 2);
  assert.match(app.el("local-graph-counts").textContent, /indexes hidden/);
  app.click("workspace-graph-open"); app.change("global-graph-indexes", true);
  assert.equal(global.nodes().length, 3); assert.equal(global.edges().length, 4);
  assert.doesNotMatch(app.el("global-graph-counts").textContent, /indexes hidden/);
  assert.equal(app.notes.has(indexPath), true);
  assert.equal(app.requests.some((request) => request.url.endsWith("/save")), false);
});

test("Partial connection warnings stay available in one compact local-graph disclosure", async (t) => {
  const app = harness(t, { "Start.md": "# Start\n[[B]]", "B.md": "# B" });
  await app.root();
  app.hooks.index = (snapshot) => { snapshot.truncated = true; snapshot.warnings = ["Workspace syntax budget reached; backlinks and graphs are partial"]; };
  await app.poll(); app.click("workspace-local-toggle");
  const statuses = app.el("workspace-context").querySelectorAll(".index-status");
  assert.equal(statuses.length, 1);
  const details = statuses[0].querySelector("details"); assert.equal(details.open, false);
  assert.equal(details.querySelector("summary").textContent, "Connections are partial");
  assert.match(details.querySelector("p").textContent, /syntax budget/);
  details.open = true; await app.poll();
  assert.equal(statuses[0].querySelector("details"), details); assert.equal(details.open, true);
  assert.match(app.el("local-graph-counts").textContent, /partial/);
});

test("Dense graph overviews fade labels until zoom provides room, with a readable hover label", async (t) => {
  const texts = { "Start.md": "# Start", ...Object.fromEntries(Array.from({ length: 120 }, (_, index) => [`Note ${index}.md`, `# Note ${index}`])) };
  const app = harness(t, texts); await app.root(); app.click("workspace-graph-open");
  const graph = app.graphs[0]; graph.emit("grab");
  graph.nodes().positions((node, index) => app.w.Object.assign(new app.w.Object(), { x: index % 11 * 20, y: Math.floor(index / 11) * 20 }));
  graph.nodes()[0].emit("free");
  assert.deepEqual({ ...graph.nodes()[120].position() }, { x: 200, y: 200 });
  graph.zoom(.99);
  const node = graph.nodes()[0]; await until(() => node.style("text-opacity") === "0", "dense overview labels");
  node.emit("mouseover"); assert.equal(node.style("text-opacity"), "1");
  assert.ok(Number.parseFloat(node.style("font-size")) * graph.zoom() >= 12);
  node.emit("mouseout"); graph.zoom(3.5);
  await until(() => node.style("text-opacity") === "1", "dense zoomed-in labels");
});

test("A successful layout retry clears its warning and busy state", async (t) => {
  const app = harness(t, { "Start.md": "# Start\n[[B]]", "B.md": "# B" });
  await app.root();
  app.hooks.layout = (_graph, options) => {
    options.onError(new Error("Graph layout timed out."));
    return { finished: Promise.resolve({ status: "error" }) };
  };
  app.click("workspace-graph-open");
  await until(() => app.el("global-graph-progress").hidden, "failed layout settled");
  assert.equal(app.el("workspace-message").hidden, false);
  assert.match(app.el("workspace-message").textContent, /Graph layout could not finish/);
  app.hooks.layout = () => ({ finished: Promise.resolve({ status: "complete" }) });
  app.change("global-graph-labels", false);
  await until(() => app.el("workspace-message").hidden, "successful retry clears warning");
  assert.equal(app.el("global-graph-canvas").getAttribute("aria-busy"), "false");
});

test("Large graph zoom retains topology without querying or rewriting every node on each input", async (t) => {
  const texts = { "Start.md": "# Start\n[[Target]]", "Target.md": "# Target", ...Object.fromEntries(Array.from({ length: 1000 }, (_, index) => [`Note ${index}.md`, `# Note ${index}`])) };
  const app = harness(t, texts); await app.root(); app.click("workspace-graph-open");
  const graph = app.graphs[0]; graph.emit("grab");
  const nextPaint = () => new Promise((resolve) => app.w.requestAnimationFrame(() => app.w.requestAnimationFrame(resolve)));
  graph.zoom(.01); await nextPaint();
  const nodes = graph.nodes(), positions = nodes.map((node) => ({ ...node.position() }));
  let queries = 0, updates = 0;
  const original = graph.nodes;
  graph.nodes = function (...args) { queries++; return original.apply(this, args); };
  nodes.on("data", () => { updates++; });
  for (let step = 1; step <= 12; step++) graph.zoom(.01 + step * .001);
  await nextPaint();
  assert.equal(queries, 0, "Zoom uses retained label geometry instead of scanning graph elements");
  assert.equal(updates, 0, "Invisible labels are not rewritten at every zoom step");
  graph.nodes = original;
  assert.equal(graph.nodes().length, 1002); assert.equal(graph.edges().length, 1);
  assert.deepEqual(graph.nodes().map((node) => ({ ...node.position() })), positions);
  assert.equal(graph.json().textureOnViewport, false);
  assert.equal(app.graphOptions[0].headless, true);
  const start = graph.getElementById("Start.md"); start.emit("mouseover");
  assert.equal(graph.getElementById("Target.md").hasClass("neighbor"), true);
  assert.equal(graph.getElementById("Note 999.md").hasClass("dimmed"), false, "Large-graph hover touches its neighborhood rather than restyling every unrelated node");
  start.emit("mouseout"); assert.equal(graph.elements(".neighbor,.hovered").length, 0);
});

test("An external edit to an inactive clean CRLF Reading tab refreshes text and index without recursion", async (t) => {
  const app = harness(t, { "Start.md": "# Start\r\n\r\n[[B]]\r\n", "B.md": "# B" });
  await app.root(); app.mode("reading"); await app.open("B.md");
  app.put("Start.md", "# Start\r\n\r\nChanged externally. [[B]]\r\n"); await app.poll();
  await app.open("Start.md"); assert.match(app.el("workspace-reader").textContent, /Changed externally/);
  assert.equal(app.el("workspace-reader").querySelectorAll("[data-note-destination]").length, 1);
  assert.equal(app.w.workspaceHasUnsavedChanges(), false);
});

test("Typing during a pending navigation refresh keeps the draft and records the external version", async (t) => {
  const app = harness(t, { "Start.md": "# Start\n\n[[Start#Start]]" }); await app.root();
  let release; const pending = new Promise((resolve) => { release = resolve; });
  app.hooks.read = () => pending; app.put("Start.md", "# Start\n\nExternal version.");
  app.click("workspace-quick-open"); app.input("workspace-quick-query", "Start.md"); app.el("workspace-quick-results").querySelector("button").click();
  await until(() => app.requests.filter((request) => request.url.endsWith("/read")).length === 2, "pending refresh");
  const view = app.view(); view.dispatch({ changes: { from: view.state.doc.length, insert: "\nTyping during refresh." } }); release();
  await until(() => !app.el("workspace-conflict").hidden, "preserved conflict");
  assert.match(view.state.doc.toString(), /Typing during refresh/); assert.doesNotMatch(view.state.doc.toString(), /External version/);
  assert.equal(app.w.workspaceHasUnsavedChanges(), true);
});

test("Reading heading navigation ignores anchors inside embedded documents", async (t) => {
  const app = harness(t, { "Start.md": "# Intro\n\n[[#Target]]\n\n## Target\n\nParagraph.\n\n![[Embed]]", "Embed.md": "# Embedded\n\nContent." });
  await app.root(); app.mode("reading");
  app.el("workspace-reader").querySelector('[data-note-destination="#Target"]').click();
  await until(() => app.el("workspace-reader").querySelector("h2")?.dataset.scrolled === "true", "actual heading scrolled");
  assert.equal(app.el("workspace-reader").querySelector(".note-embed h1").dataset.scrolled, undefined);
});
