import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { JSDOM, VirtualConsole } from "jsdom";

const asset = (name) => readFileSync(new URL(`../../src/auto_zettelkasten/web/${name}`, import.meta.url), "utf8");
const revision = "2".repeat(64), digest = "1".repeat(64);
const defaults = () => ({ version: 1, colorBy: "none", origins: { managed: true, personal: true, unknown: true } });
async function until(check, description) {
  const deadline = Date.now() + 3000;
  while (!check()) {
    assert.ok(Date.now() < deadline, description);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

function harness(t, { canvas = true, saved = {}, extraUnknown = 0 } = {}) {
  const errors = [], requests = [], layouts = [], graphs = new Map(), views = new Map(), hooks = {};
  const console = new VirtualConsole(); console.on("jsdomError", (error) => errors.push(error));
  const dom = new JSDOM(asset("index.html"), { url: "http://127.0.0.1:8765", runScripts: "dangerously", pretendToBeVisual: true, virtualConsole: console });
  const w = dom.window;
  w.HTMLElement.prototype.scrollIntoView = function () {};
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; };
  w.Range.prototype.getClientRects = () => [];
  w.Range.prototype.getBoundingClientRect = () => ({ left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 });
  w.setInterval = () => 1;
  const notes = [
    { path: "A.md", note_origin: "managed", editable: false, override_available: true, text: "# A\n[[Personal]]\n" },
    { path: "B.md", note_origin: "managed", editable: false, override_available: true, text: "# B\n" },
    { path: "Personal.md", note_origin: "personal", editable: true, text: "# Personal\n" },
    // Editable files without a verified origin still belong in the unknown category.
    { path: "Unknown.md", editable: true, text: "# Unknown\n" },
    ...Array.from({ length: extraUnknown }, (_, index) => ({ path: `Extra ${index}.md`, editable: true, text: `# Extra ${index}\n` })),
  ].map((row) => ({ ...row, id: row.path, name: row.path, revision, reason: "", recovery_key: row.path }));
  const semantic_map = { status: "current", identity_digest: digest, note_revisions: { "A.md": revision, "B.md": revision },
    communities: [{ community_id: "semantic-ab", algorithm: "leiden", paths: ["A.md", "B.md"] }] };
  const relations = [
    { id: "accepted", source: "A.md", target: "Personal.md", type: "supports" },
    { id: "citation", source: "A.md", target: "Unknown.md", type: "cites" },
    { id: "semantic:ab", source: "A.md", target: "B.md", type: "semantic_similarity", derived: true, accepted: false,
      evidence_backed: false, decision_status: "suggested", score: .75,
      provenance: { embedding_identity: digest, source_chunk_ids: ["a-chunk"], target_chunk_ids: ["b-chunk"] } },
  ];
  const entries = notes.map((note) => ({ path: note.path, name: note.name, kind: "file", editable: note.editable }));
  const preferences = new Map(Object.entries(saved)), workspacePaths = new Map();
  let openedPath, generation = 0;
  const workspace = () => ({ id: `workspace:${generation}`, path: openedPath, name: "Synthetic notes", editable: true, entries, truncated: false });
  w.fetch = async (url, request) => {
    const body = JSON.parse(request?.body || "{}"); requests.push({ url, body });
    let result;
    if (url.endsWith("/graph-preferences")) {
      assert.equal(body.workspace_id, workspace().id, "Old workspace IDs are invalid after opening another folder");
      const path = workspacePaths.get(body.workspace_id);
      if (body.preferences) { await hooks.write?.(body, path); preferences.set(path, structuredClone(body.preferences)); }
      else await hooks.load?.(body, path);
      result = { preferences: preferences.get(path) || defaults() };
    } else if (url.endsWith("/open")) {
      openedPath = body.path; generation += 1; workspacePaths.set(workspace().id, openedPath); result = { workspace: workspace() };
    } else if (url.endsWith("/tree")) result = { workspace: workspace() };
    else if (url.endsWith("/index")) result = { generation: "g1", complete: true, documents: notes, entries, relations, semantic_map, warnings: [], truncated: false };
    else if (url.endsWith("/read")) result = { document: notes.find((row) => row.path === body.path) };
    else throw new Error(`Unexpected request ${url}`);
    return { ok: true, status: 200, json: async () => structuredClone(result) };
  };
  const load = (name) => { const script = w.document.createElement("script"); script.textContent = asset(name); w.document.body.append(script); };
  load("workspace-vendor.js");
  const kit = w.WorkspaceKit;
  w.WorkspaceKit = { ...kit,
    LibraryParser: class { async parse(documents) { return documents.map(kit.parseLibraryDocument); } close() {} },
    cytoscape(options) {
      const config = w.Object.assign(new w.Object(), options), mode = options.container.id.startsWith("local") ? "local" : "global";
      config.container = undefined; config.headless = true;
      const graph = kit.cytoscape(config); graphs.set(mode, graph); return graph;
    },
    layoutGraph(graph, options) { layouts.push({ graph, options }); return { finished: Promise.resolve({ status: "complete" }), cancel() {} }; },
    createGraphView(graph, options) {
      if (!canvas) return null;
      const view = { appearance: [options], setAppearance(value) { this.appearance.push(value); }, setOptions() {}, resize() {}, zoomBy() {} };
      views.set(graph, view); return view;
    },
  };
  load("app.js"); load("workspace.js");
  const el = (id) => w.document.getElementById(id), click = (id) => el(id).click();
  const change = (id, value) => {
    const control = el(id); if (control.type === "checkbox") control.checked = value; else control.value = value;
    control.dispatchEvent(new w.Event("change", { bubbles: true }));
  };
  const openNote = async () => {
    click("workspace-quick-open"); el("workspace-quick-query").value = "A.md";
    el("workspace-quick-query").dispatchEvent(new w.Event("input", { bubbles: true }));
    el("workspace-quick-query").dispatchEvent(new w.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await until(() => el("workspace-breadcrumbs").title === "A.md", "Opened synthetic source");
  };
  const open = async (path = "/synthetic-one") => {
    const before = requests.filter((row) => row.url.endsWith("/index")).length;
    assert.equal(await w.openMemoryWorkspace(path), true);
    await until(() => requests.filter((row) => row.url.endsWith("/index")).length > before && !/^(Checking|Indexing|Verifying)/.test(el("workspace-index-status").textContent), "Synthetic index complete");
  };
  const global = () => { click("workspace-graph-open"); return graphs.get("global"); };
  const local = async () => { await openNote(); click("workspace-local-toggle"); return graphs.get("local"); };
  t.after(() => { for (const graph of graphs.values()) if (!graph.destroyed()) graph.destroy(); w.close(); assert.deepEqual(errors, []); });
  return { w, kit, el, click, change, graphs, views, layouts, notes, relations, preferences, requests, hooks, open, global, local };
}

test("Origin checkboxes combine independently, retain edge-layer choices and agree in local/global graphs", async (t) => {
  const app = harness(t); await app.open(); const graph = app.global(), local = await app.local();
  assert.equal(graph.nodes().length, 4);
  assert.equal(local.nodes().length, 2);
  assert.deepEqual(["managed", "personal", "unknown"].map((origin) => app.el(`global-graph-origin-${origin}`).checked), [true, true, true]);
  const accepted = graph.edges().map((edge) => edge.id()), originals = JSON.stringify([app.notes, app.relations]);
  app.change("global-graph-citations", true); app.change("global-graph-discovery", true); app.change("global-graph-references", true);
  const edges = graph.edges().map((edge) => edge.id()); assert.ok(edges.length > accepted.length);
  app.change("global-graph-color-by", "origin");
  assert.equal(app.el("local-graph-color-by").value, "origin");
  assert.match(app.el("global-graph-color-legend").textContent, /Managed sources \(2\)/);
  assert.match(app.el("global-graph-color-legend").textContent, /Verified personal \(1\)/);
  assert.match(app.el("global-graph-color-legend").textContent, /unknown.*\(1\)/i);
  app.change("global-graph-origin-managed", false);
  assert.equal(graph.nodes().length, 2); assert.equal(local.nodes().length, 1);
  assert.equal(app.el("local-graph-origin-managed").checked, false);
  assert.ok(graph.nodes().every((node) => node.data("note_origin") !== "managed"));
  for (const origin of ["personal", "unknown"]) app.change(`local-graph-origin-${origin}`, false);
  assert.equal(graph.nodes().length, 0); assert.equal(local.nodes().length, 0);
  assert.match(app.el("global-graph-empty").textContent, /No notes match/);
  for (const origin of ["managed", "personal", "unknown"]) app.change(`local-graph-origin-${origin}`, true);
  assert.deepEqual(graph.edges().map((edge) => edge.id()).sort(), edges.sort());
  for (const layer of ["citations", "discovery", "references", "relations"]) assert.equal(app.el(`global-graph-${layer}`).checked, true);
  assert.equal(JSON.stringify([app.notes, app.relations]), originals, "Display settings never rewrite notes or accepted records");
});

test("Every origin-filter combination selects the expected notes without inferring personal authorship", async (t) => {
  const app = harness(t); await app.open(); const graph = app.global();
  const selected = ["managed", "personal", "unknown"];
  for (let mask = 0; mask < 8; mask += 1) {
    selected.forEach((origin, bit) => app.change(`global-graph-origin-${origin}`, Boolean(mask & 1 << bit)));
    const expected = [mask & 1 ? ["A.md", "B.md"] : [], mask & 2 ? ["Personal.md"] : [], mask & 4 ? ["Unknown.md"] : []].flat().sort();
    assert.deepEqual(Array.from(graph.nodes(), (node) => node.data("path")).sort(), expected);
    assert.ok(graph.edges().every((edge) => graph.getElementById(edge.data("source")).length && graph.getElementById(edge.data("target")).length));
  }
});

test("Recoloring and theme changes reuse positions, viewport and topology without asking for a new layout", async (t) => {
  const app = harness(t); await app.open(); const graph = app.global();
  graph.nodes().forEach((node, index) => node.position({ x: 91 + index * 11, y: 27 + index * 17 }));
  const pan = app.w.Object.assign(new app.w.Object(), { x: 74, y: 81 });
  graph.viewport({ zoom: 2, pan });
  const positions = JSON.stringify(graph.nodes().map((node) => node.position())), edges = graph.edges().map((edge) => edge.id());
  const layouts = app.layouts.length;
  app.change("global-graph-color-by", "community");
  assert.equal(app.layouts.length, layouts);
  assert.equal(JSON.stringify(graph.nodes().map((node) => node.position())), positions);
  assert.equal(graph.zoom(), 2); assert.deepEqual({ ...graph.pan() }, { x: 74, y: 81 });
  assert.deepEqual(graph.edges().map((edge) => edge.id()), edges);
  const node = graph.nodes().find((node) => node.data("path") === "A.md");
  const light = node.data("graph_color");
  app.w.document.documentElement.dataset.theme = "dark";
  await until(() => app.views.get(graph).appearance.at(-1).dark, "Dark graph palette applied");
  assert.notEqual(node.data("graph_color"), light);
  assert.equal(node.data("graph_color"), app.kit.graphNodeAppearance(node.data(), { colorBy: "community", dark: true }).color);
  assert.equal(app.layouts.length, layouts);
  const row = app.el("global-graph-list").querySelector("button");
  assert.match(row.textContent, /Community/);
  assert.equal(row.querySelector(".graph-color-swatch").style.backgroundColor, app.el("global-graph-color-legend").querySelector(".graph-color-swatch").style.backgroundColor);
  assert.ok(app.el("global-graph-color-legend").getAttribute("aria-label"));
});

test("Workspace preferences survive reopening and an intentionally empty origin filter is not reset", async (t) => {
  const app = harness(t); await app.open(); app.global();
  app.change("global-graph-color-by", "origin");
  for (const origin of ["managed", "personal", "unknown"]) app.change(`global-graph-origin-${origin}`, false);
  await until(() => app.preferences.get("/synthetic-one")?.origins.unknown === false, "Latest preferences saved");
  await app.open("/synthetic-two"); app.global();
  assert.equal(app.el("global-graph-color-by").value, "none"); assert.equal(app.graphs.get("global").nodes().length, 4);
  await app.open("/synthetic-one"); const graph = app.global();
  assert.equal(app.el("global-graph-color-by").value, "origin"); assert.equal(graph.nodes().length, 0);
  assert.ok(app.requests.filter((row) => row.url.endsWith("/graph-preferences")).every((row) => row.body.workspace_id));
  assert.equal(app.preferences.get("/synthetic-one").version, 1);
});

test("Preference writes remain ordered across failures and old workspace responses cannot replace current settings", async (t) => {
  const app = harness(t); await app.open(); app.global();
  let release, count = 0;
  app.hooks.write = async () => { count += 1; if (count === 1) { await new Promise((resolve) => { release = resolve; }); throw new Error("Synthetic disk failure"); } };
  app.change("global-graph-color-by", "origin");
  app.change("global-graph-origin-unknown", false);
  await until(() => release, "First write is in flight");
  assert.equal(count, 1, "A second write waits for the first"); release();
  await until(() => app.preferences.get("/synthetic-one")?.origins.unknown === false, "Queued write succeeds after failure");
  assert.equal(app.el("global-graph-preferences-status").hidden, true);
  app.hooks.write = async () => { await new Promise((resolve) => { release = resolve; }); throw new Error("Old workspace failure"); };
  release = undefined; app.change("global-graph-color-by", "community");
  await until(() => release, "Old workspace write pending");
  const opening = app.open("/synthetic-two"); release(); await opening; app.global();
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(app.el("global-graph-color-by").value, "none");
  assert.equal(app.el("global-graph-preferences-status").hidden, true);
});

test("A failed preference write is visible while the graph remains usable", async (t) => {
  const app = harness(t); await app.open(); const graph = app.global();
  app.hooks.write = () => { throw new Error("Synthetic preferences unavailable"); };
  app.change("global-graph-color-by", "origin");
  await until(() => !app.el("global-graph-preferences-status").hidden, "Save failure reported");
  assert.match(app.el("global-graph-preferences-status").textContent, /could not be saved.*this session/);
  assert.equal(graph.nodes().length, 4); assert.equal(app.el("global-graph-color-by").value, "origin");
});

test("Opening another workspace flushes both queued writes before invalidating the old document-service ID", async (t) => {
  const app = harness(t); await app.open(); app.global();
  let release, count = 0;
  app.hooks.write = async () => { count += 1; if (count === 1) await new Promise((resolve) => { release = resolve; }); };
  app.change("global-graph-color-by", "origin");
  app.change("global-graph-color-by", "community");
  await until(() => release, "Outgoing write pending");
  const opens = app.requests.filter((row) => row.url.endsWith("/open")).length;
  const opening = app.open("/synthetic-two");
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(app.requests.filter((row) => row.url.endsWith("/open")).length, opens, "Folder switch waits for queued display settings");
  release(); await opening; app.global();
  assert.equal(count, 2); assert.equal(app.preferences.get("/synthetic-one").colorBy, "community");
  await app.open("/synthetic-one"); app.global();
  assert.equal(app.el("global-graph-color-by").value, "community");
});

test("Pending preferences finish before workspace switching; malformed versions use all-visible defaults", async (t) => {
  const app = harness(t, { saved: { "/synthetic-one": { version: 37, colorBy: "origin", origins: { managed: false } } } });
  let release; app.hooks.load = async () => new Promise((resolve) => { release = resolve; });
  const opening = app.w.openMemoryWorkspace("/synthetic-one");
  await until(() => release, "Workspace preferences pending");
  assert.equal(await app.w.openMemoryWorkspace("/synthetic-two"), false, "An in-progress workspace open cannot be raced");
  release(); assert.equal(await opening, true); app.hooks.load = undefined;
  await until(() => !/^(Checking|Indexing|Verifying)/.test(app.el("workspace-index-status").textContent), "First index complete");
  const graph = app.global(); assert.equal(graph.nodes().length, 4); assert.equal(app.el("global-graph-color-by").value, "none");
  await app.open("/synthetic-two"); assert.equal(app.global().nodes().length, 4);
});

test("Canvas failure exposes a visible, labeled note list with shared category colors", async (t) => {
  const app = harness(t, { canvas: false }); await app.open(); app.global();
  app.change("global-graph-color-by", "origin");
  assert.equal(app.el("global-graph-canvas").hidden, true);
  assert.equal(app.el("global-graph-list").hidden, false);
  assert.equal(app.el("global-graph-fallback").hidden, false);
  assert.equal(app.el("global-graph-show-list").getAttribute("aria-expanded"), "true");
  assert.equal(app.el("global-graph-show-list").disabled, true);
  const rows = [...app.el("global-graph-list").querySelectorAll("button")];
  assert.equal(rows.length, 4);
  assert.match(rows.find((row) => row.textContent.startsWith("Unknown")).textContent, /unknown/i);
  assert.ok(rows.every((row) => row.querySelector(".graph-color-swatch") && row.querySelector(".graph-list-category")));
  rows[0].click(); await until(() => app.el("workspace-breadcrumbs").title === "A.md", "Fallback keyboard button opens note");
});

test("Large Canvas fallback lists render in bounded pages while retaining all graph notes", async (t) => {
  const app = harness(t, { canvas: false, extraUnknown: 1050 }); await app.open(); const graph = app.global();
  assert.equal(graph.nodes().length, 1054); assert.equal(app.el("global-graph-list").hidden, false);
  assert.equal(app.el("global-graph-list").querySelectorAll(":scope > button").length, 201);
  app.el("global-graph-list").querySelector(".graph-list-more").click();
  assert.equal(app.el("global-graph-list").querySelectorAll(":scope > button").length, 401);
  app.change("global-graph-origin-unknown", false);
  assert.equal(graph.nodes().length, 3); assert.equal(app.el("global-graph-list").querySelectorAll(":scope > button").length, 3);
});

test("Community selection and focus keep bridges, viewport, layer choices and note navigation", async (t) => {
  const app = harness(t); await app.open(); const graph = app.global();
  graph.nodes().forEach((node, at) => node.position({ x: 91 + at * 41, y: 71 + at * 17 }));
  graph.viewport({ zoom: 2, pan: app.w.Object.assign(new app.w.Object(), { x: 74, y: 81 }) });
  const layouts = app.layouts.length, positions = new Map(graph.nodes().map((node) => [node.id(), { ...node.position() }]));
  app.change("global-graph-community", "semantic-ab");
  assert.equal(app.el("global-graph-community-panel").hidden, false);
  assert.match(app.el("global-graph-community-panel").textContent, /2 visible members/);
  assert.equal(graph.nodes().length, 4, "Selecting alone does not filter the graph");
  app.click("global-graph-community-focus");
  assert.deepEqual(Array.from(graph.nodes(), (node) => node.id()).sort(), ["A.md", "B.md", "Personal.md"]);
  assert.equal(graph.getElementById("Personal.md").data("community_bridge"), true);
  assert.equal(graph.zoom(), 2); assert.deepEqual({ ...graph.pan() }, { x: 74, y: 81 });
  assert.equal(app.layouts.length, layouts, "Focus does not restart the force layout");
  for (const node of graph.nodes()) assert.deepEqual({ ...node.position() }, positions.get(node.id()));
  assert.equal(app.el("global-graph-discovery").checked, false);
  assert.match(app.el("global-graph-community-panel").textContent, /1 directly connected external notes remain visible/);
  app.click("global-graph-community-focus");
  assert.equal(graph.nodes().length, 4);
  assert.equal(app.layouts.length, layouts);
  app.el("global-graph-community-panel").querySelector(".graph-community-members > button").click();
  await until(() => app.el("workspace-breadcrumbs").title === "A.md", "Community member opens its existing note");
});
