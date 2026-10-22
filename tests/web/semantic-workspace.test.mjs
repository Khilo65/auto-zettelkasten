import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { JSDOM, VirtualConsole } from "jsdom";

const asset = (name) => readFileSync(new URL(`../../src/auto_zettelkasten/web/${name}`, import.meta.url), "utf8");
const revision = "2".repeat(64), digest = "1".repeat(64);
async function until(check, description) {
  const deadline = Date.now() + 3000;
  while (!check()) {
    assert.ok(Date.now() < deadline, description);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

function harness(t, { recovery = false } = {}) {
  const errors = [], polls = [], requests = [], graphs = new Map();
  const console = new VirtualConsole(); console.on("jsdomError", (error) => errors.push(error));
  const dom = new JSDOM(asset("index.html"), { url: "http://127.0.0.1:8765", runScripts: "dangerously", pretendToBeVisual: true, virtualConsole: console });
  const w = dom.window;
  w.HTMLElement.prototype.scrollIntoView = function () {};
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; };
  w.Range.prototype.getClientRects = () => [];
  w.Range.prototype.getBoundingClientRect = () => ({ left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 });
  w.setInterval = (callback) => { polls.push(callback); return polls.length; };
  const notes = new Map(["A", "B", "C", "D", "Personal"].map((name) => {
    const path = `${name}.md`, personal = name === "Personal";
    return [path, { id: path, path, name: path, text: `# ${name}\n${name === "A" ? "![[B]]\n" : "Original body.\n"}`, revision,
      editable: personal, override_available: !personal, reason: personal ? "" : "Generated", recovery_key: path }];
  }));
  const semantic_map = { status: "current", identity_digest: digest,
    note_revisions: Object.fromEntries(["A.md", "B.md", "C.md", "D.md"].map((path) => [path, revision])),
    communities: [{ community_id: "semantic-ab", algorithm: "leiden", paths: ["A.md", "B.md"] },
      { community_id: "semantic-cd", algorithm: "leiden", paths: ["C.md", "D.md"] }] };
  const relations = [["A.md", "B.md"], ["C.md", "D.md"]].map(([source, target], index) => ({
    id: `semantic:${index}`, source, target, type: "semantic_similarity", derived: true, accepted: false,
    evidence_backed: false, decision_status: "suggested", score: .75,
    provenance: { embedding_identity: digest, source_chunk_ids: [source], target_chunk_ids: [target] },
  }));
  relations.push({ id: "accepted", source: "A.md", target: "B.md", type: "supports" },
    { id: "citation", source: "A.md", target: "B.md", type: "cites" });
  const workspace = { id: "root", path: "/notes", name: "Notes", editable: true, entries: [], truncated: false };
  let generation = 1, rebuilds = 0;
  w.fetch = async (url, request) => {
    const body = JSON.parse(request?.body || "{}"); requests.push({ url, body });
    let result;
    if (url.endsWith("/open") || url.endsWith("/tree")) result = { workspace };
    else if (url.endsWith("/index")) result = body.known_generation === `g${generation}`
      ? { generation: `g${generation}`, complete: true, not_modified: true }
      : { generation: `g${generation}`, complete: true, documents: [...notes.values()],
        entries: [...notes.values()].map((note) => ({ path: note.path, name: note.name, kind: "file", editable: note.editable })),
        relations, semantic_map, warnings: [], truncated: false };
    else if (url.endsWith("/read")) result = { document: notes.get(body.path) };
    else throw new Error(`Unexpected request ${url}`);
    return { ok: true, status: 200, json: async () => structuredClone(result) };
  };
  const load = (name) => { const script = w.document.createElement("script"); script.textContent = asset(name); w.document.body.append(script); };
  load("workspace-vendor.js");
  const kit = w.WorkspaceKit;
  w.WorkspaceKit = { ...kit,
    buildLibraryIndex(...args) { rebuilds += 1; return kit.buildLibraryIndex(...args); },
    LibraryParser: class { async parse(documents) { return documents.map(kit.parseLibraryDocument); } close() {} },
    cytoscape: (options) => {
      const config = w.Object.assign(new w.Object(), options), mode = options.container.id.startsWith("local") ? "local" : "global";
      config.container = undefined; config.headless = true;
      const graph = kit.cytoscape(config); graphs.set(mode, graph); return graph;
    },
    layoutGraph: () => ({ finished: Promise.resolve({ status: "complete" }), cancel() {} }),
  };
  if (recovery) w.localStorage.setItem("az.workspace.draft.A.md", JSON.stringify({ text: "# A\nRecovered edit", revision }));
  load("app.js"); load("workspace.js");
  const el = (id) => w.document.getElementById(id), click = (id) => el(id).click();
  const input = (id, value) => { el(id).value = value; el(id).dispatchEvent(new w.Event("input", { bubbles: true })); };
  const open = async (path) => {
    click("workspace-quick-open"); input("workspace-quick-query", path);
    el("workspace-quick-query").dispatchEvent(new w.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await until(() => el("workspace-breadcrumbs").title === path && el("workspace-global-graph").hidden, `Opened ${path}`);
  };
  const graph = (mode = "global") => {
    if (mode === "global") click("workspace-graph-open");
    else if (!graphs.has("local")) click("workspace-local-toggle");
    el(`${mode}-graph-discovery`).checked = true; el(`${mode}-graph-relations`).checked = false;
    el(`${mode}-graph-discovery`).dispatchEvent(new w.Event("change", { bubbles: true }));
    return graphs.get(mode);
  };
  const edit = async (text) => {
    const view = kit.EditorView.findFromDOM(el("workspace-editor").querySelector(".cm-editor"));
    const before = rebuilds;
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
    await until(() => rebuilds > before, "Draft links refreshed");
  };
  t.after(() => { for (const graph of graphs.values()) graph.destroy(); w.close(); assert.deepEqual(errors, []); });
  return { w, el, click, notes, requests, graphs, open, graph, edit, poll: () => polls[0](),
    changeDisk(path) { notes.set(path, { ...notes.get(path), text: "# Changed source\n", revision: "3".repeat(64) }); generation += 1; },
    async root() {
      click("workspace-welcome-open"); input("workspace-folder-path", "/notes");
      el("workspace-folder-form").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
      await until(() => el("workspace-tree").textContent.includes("Personal"), "Complete index");
    },
  };
}

const derivedPairs = (graph) => [...graph.edges().filter((edge) => edge.data("derived")).map((edge) => `${edge.data("source")}>${edge.data("target")}`)].sort();
const memberships = (graph, path) => JSON.parse(JSON.stringify(graph.getElementById(path).data("semantic_communities") || []));

test("Reading unchanged embedded notes and polling preserve semantic neighbors and communities in both graphs", async (t) => {
  const app = harness(t); await app.root();
  const global = app.graph(); assert.deepEqual(derivedPairs(global), ["A.md>B.md", "C.md>D.md"]);
  await app.open("A.md"); app.w.document.querySelector('[data-mode="reading"]').click();
  await until(() => app.el("workspace-reader").textContent.includes("Original body."), "Embed hydrated");
  const local = app.graph("local");
  assert.deepEqual(derivedPairs(local), ["A.md>B.md"]);
  assert.deepEqual(memberships(local, "A.md"), [{ community_id: "semantic-ab", algorithm: "leiden" }]);
  app.graph(); assert.deepEqual(derivedPairs(global), ["A.md>B.md", "C.md>D.md"]);
  await app.poll();
  assert.deepEqual(derivedPairs(global), ["A.md>B.md", "C.md>D.md"]);
  assert.deepEqual(memberships(global, "B.md"), [{ community_id: "semantic-ab", algorithm: "leiden" }]);
  assert.equal(app.w.workspaceHasUnsavedChanges(), false);
  assert.ok(app.requests.some((request) => request.body.known_generation === "g1"));
});

test("An unrelated personal draft preserves every mapped neighbor and community across unchanged polls", async (t) => {
  const app = harness(t); await app.root(); await app.open("Personal.md");
  await app.edit("# Personal\nAn unsaved research question.");
  assert.equal(app.w.workspaceHasUnsavedChanges(), true);
  const global = app.graph();
  assert.deepEqual(derivedPairs(global), ["A.md>B.md", "C.md>D.md"]);
  assert.deepEqual(memberships(global, "A.md"), [{ community_id: "semantic-ab", algorithm: "leiden" }]);
  await app.poll(); assert.deepEqual(derivedPairs(global), ["A.md>B.md", "C.md>D.md"]);
  await app.open("C.md");
  assert.deepEqual(derivedPairs(app.graph("local")), ["C.md>D.md"]);
  assert.equal(app.w.workspaceHasUnsavedChanges(), true);
});

test("A mapped draft suppresses only its stale suggestions and memberships, and undo restores them", async (t) => {
  const app = harness(t); await app.root(); const global = app.graph(); await app.open("A.md");
  const local = app.graph("local"); assert.deepEqual(derivedPairs(local), ["A.md>B.md"]);
  await app.edit("# A\nA personal correction to this mapped source.");
  assert.deepEqual(derivedPairs(local), []);
  assert.deepEqual(memberships(local, "A.md"), []);
  app.graph();
  await until(() => derivedPairs(global).length === 1, "Changed source suggestions removed");
  assert.deepEqual(derivedPairs(global), ["C.md>D.md"]);
  assert.deepEqual(memberships(global, "A.md"), []);
  assert.deepEqual(memberships(global, "C.md"), [{ community_id: "semantic-cd", algorithm: "leiden" }]);
  app.el("global-graph-relations").click(); app.el("global-graph-citations").click();
  assert.ok(global.edges().some((edge) => edge.data("type") === "supports"));
  assert.ok(global.edges().some((edge) => edge.data("type") === "cites"));
  await app.poll(); assert.deepEqual(derivedPairs(global), ["C.md>D.md"]);
  await app.open("A.md");
  app.graph("local"); assert.deepEqual(derivedPairs(local), []);
  await app.edit(app.notes.get("A.md").text);
  await until(() => derivedPairs(local).length === 1, "Undo restored source suggestions");
  assert.deepEqual(derivedPairs(app.graph()), ["A.md>B.md", "C.md>D.md"]);
});

test("A changed mapped source revision stays stale across refresh and unchanged polls without hiding other sources", async (t) => {
  const app = harness(t); await app.root(); await app.open("A.md");
  const global = app.graph(); app.changeDisk("A.md"); await app.poll();
  await until(() => derivedPairs(global).length === 1, "New revision invalidated source suggestions");
  assert.deepEqual(derivedPairs(global), ["C.md>D.md"]);
  assert.deepEqual(memberships(global, "A.md"), []);
  assert.deepEqual(memberships(global, "C.md"), [{ community_id: "semantic-cd", algorithm: "leiden" }]);
  await app.poll(); assert.deepEqual(derivedPairs(global), ["C.md>D.md"]);
  await app.open("A.md"); assert.deepEqual(derivedPairs(app.graph("local")), []);
  assert.equal(app.w.workspaceHasUnsavedChanges(), false);
});

test("Pending mapped recovery excludes only that source until the draft is reviewed", async (t) => {
  const app = harness(t, { recovery: true }); await app.root(); await app.open("A.md");
  assert.equal(app.el("workspace-recovery").hidden, false);
  const global = app.graph();
  assert.deepEqual(derivedPairs(global), ["C.md>D.md"]);
  assert.deepEqual(memberships(global, "A.md"), []);
  assert.deepEqual(memberships(global, "D.md"), [{ community_id: "semantic-cd", algorithm: "leiden" }]);
  await app.poll(); assert.deepEqual(derivedPairs(global), ["C.md>D.md"]);
});

test("An unresolved disk change stays stale even when the draft returns to its previously saved text", async (t) => {
  const app = harness(t); await app.root(); const global = app.graph(); await app.open("A.md");
  const original = app.notes.get("A.md").text;
  await app.edit("# A\nAn unsaved correction.");
  app.changeDisk("A.md"); await app.poll();
  assert.equal(app.el("workspace-conflict").hidden, false);
  await app.edit(original);
  assert.equal(app.w.workspaceHasUnsavedChanges(), true, "The unresolved disk change still requires review");
  const local = app.graph("local");
  assert.deepEqual(derivedPairs(local), []);
  assert.deepEqual(memberships(local, "A.md"), []);
  app.graph(); assert.deepEqual(derivedPairs(global), ["C.md>D.md"]);
  await app.poll(); assert.deepEqual(derivedPairs(global), ["C.md>D.md"]);
});
