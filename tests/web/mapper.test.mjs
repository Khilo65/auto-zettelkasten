import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { JSDOM, VirtualConsole } from "jsdom";

const asset = (name) => readFileSync(new URL(`../../src/auto_zettelkasten/web/${name}`, import.meta.url), "utf8");
async function until(check, label) {
  for (let attempt = 0; attempt < 400; attempt += 1) {
    if (check()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.fail(`Timed out: ${label}`);
}

const master = "02_source_memory/indexes/INDEX.md";
const atomic = "02_source_memory/notes/Atomic note.md";
const collections = [{ key: "PARENT", name: "Thesis", path: "Thesis" }, { key: "CHILD", name: "Reading", path: "Thesis / Reading", parent_key: "PARENT" }];
const completed = { status: "completed", workspace: "/memory", memory_workspace: "/memory", workspace_available: true, report: { inventory_count: 2, validated_note_count: 2, typed_relation_count: 1 } };

function harness(t, options = {}) {
  const errors = [], virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (error) => errors.push(error));
  const dom = new JSDOM(asset("index.html"), { url: "http://127.0.0.1:8765", runScripts: "dangerously", pretendToBeVisual: true, virtualConsole });
  const w = dom.window;
  w.HTMLElement.prototype.scrollIntoView = function () {};
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; };
  w.Range.prototype.getClientRects = () => [];
  w.Range.prototype.getBoundingClientRect = () => ({ left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 });
  const requests = [], polls = new Map();
  const interval = w.setInterval.bind(w);
  w.setInterval = (callback, delay) => { polls.set(delay, callback); return interval(callback, delay); };
  let opened = "/personal";
  const notes = (path) => path === "/personal" ? [{ path: "Draft.md", text: "# Draft\n", editable: true }] : [
    { path: master, text: "# Source Index\n\n[[../notes/Atomic note|Atomic note]]\n", editable: false },
    { path: atomic, text: "# Atomic note\n\nA supported finding.\n", editable: false },
  ];
  const document = (path) => {
    const note = notes(opened).find((doc) => doc.path === path);
    assert.ok(note, `Known test note ${path}`);
    return { ...note, id: `${opened}:${path}`, name: path.split("/").at(-1), revision: "v1", bom: false, newline: "lf", recovery_key: `${opened}:${path}`, reason: note.editable ? "" : "Generated workspace or engine-owned note is read-only." };
  };
  const workspace = () => {
    const entries = new Map();
    for (const doc of notes(opened)) {
      const parts = doc.path.split("/");
      for (let depth = 1; depth < parts.length; depth += 1) {
        const path = parts.slice(0, depth).join("/");
        entries.set(path, { path, name: parts[depth - 1], kind: "directory", editable: doc.editable });
      }
      entries.set(doc.path, { path: doc.path, name: parts.at(-1), kind: "file", editable: doc.editable });
    }
    return { id: opened, path: opened, name: opened.slice(1), editable: opened === "/personal", reason: opened === "/personal" ? "" : "Generated workspace is read-only.", entries: [...entries.values()], truncated: false };
  };
  w.fetch = async (url, request) => {
    const body = JSON.parse(request.body || "{}"); requests.push({ url, body });
    let result;
    if (url === "/api/state") result = { auth: { ready: true, method: "codex", status: "ready" }, run: options.run || { status: "idle" }, preferences: {}, default_output_folder: "/output" };
    else if (url === "/api/collections") result = { collections: options.collections || collections };
    else if (url === "/api/cancel") result = { status: "cancelled" };
    else if (url === "/api/estimate") {
      const selected = collections.find((row) => row.key === body.collection_key);
      const source = body.scope === "library" ? { scope: "library", name: "Entire Zotero library", path: "Entire Zotero library", include_subcollections: false } : { ...selected, scope: "collection", include_subcollections: body.include_subcollections };
      result = { estimate_id: "estimate-1", source, collection: source, workspace: "/memory", estimate: { provider: "codex", inventory_count: 2, new_source_jobs: 2, reusable_or_existing_records: 0, source_calls: { expected: 2 }, graph_calls: { expected: 1 } } };
    } else if (url === "/api/approve") result = { status: "running", workspace: "/memory" };
    else if (url === "/api/run") result = completed;
    else if (url === "/api/workspace/open") { opened = body.path; result = { workspace: workspace() }; }
    else if (url === "/api/workspace/tree") result = { workspace: workspace() };
    else if (url === "/api/workspace/read") result = { document: document(body.path) };
    else if (url === "/api/workspace/index") result = { documents: notes(opened).map((doc) => document(doc.path)), relations: [], truncated: false, warnings: [] };
    else throw new Error(`Unexpected request ${url}`);
    return { ok: true, status: 200, json: async () => structuredClone(result) };
  };
  for (const name of ["workspace-vendor.js", "app.js", "workspace.js"]) {
    const script = w.document.createElement("script"); script.textContent = asset(name); w.document.body.append(script);
  }
  const el = (id) => w.document.getElementById(id), click = (id) => el(id).click();
  const change = (id, value) => { const field = el(id); if (field.type === "checkbox") field.checked = value; else field.value = value; field.dispatchEvent(new w.Event("change", { bubbles: true })); };
  const scope = (value) => { const radio = w.document.querySelector(`[name="source-scope"][value="${value}"]`); radio.checked = true; radio.dispatchEvent(new w.Event("change", { bubbles: true })); };
  const mapper = async () => {
    click("show-mapping");
    await until(() => requests.some((request) => request.url === "/api/collections") && !el("find-zotero").disabled, "mapper discovery");
  };
  const estimate = async () => {
    el("mapping-form").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
    await until(() => !el("review").hidden, "estimate review");
  };
  const personal = async () => {
    click("workspace-welcome-open"); el("workspace-folder-path").value = "/personal";
    el("workspace-folder-form").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
    await until(() => w.document.querySelector('[data-path="Draft.md"]'), "personal tree");
    w.document.querySelector('[data-path="Draft.md"]').click();
    await until(() => w.document.querySelector(".cm-editor"), "personal editor");
    return w.WorkspaceKit.EditorView.findFromDOM(w.document.querySelector(".cm-editor"));
  };
  t.after(() => { w.close(); assert.deepEqual(errors, []); });
  return { w, el, click, change, scope, mapper, estimate, personal, requests, polls };
}

test("the labeled mapper opens source choices before connection without contacting a provider", async (t) => {
  const app = harness(t);
  assert.equal(app.requests.length, 0);
  assert.equal(app.el("show-mapping").getAttribute("aria-label"), "Auto-Zettelkasten mapper");
  assert.equal(app.el("show-mapping").textContent, "Mapper");
  await app.mapper();
  assert.equal(app.el("mapping-screen").hidden, false);
  assert.equal(app.el("show-mapping").getAttribute("aria-pressed"), "true");
  assert.equal(app.w.document.activeElement, app.el("mapper-title"));
  assert.ok(app.el("collection-title").compareDocumentPosition(app.el("connect-title")) & app.w.Node.DOCUMENT_POSITION_FOLLOWING);
  assert.deepEqual(app.requests.map((request) => request.url), ["/api/state", "/api/collections"]);
});

test("library mapping works with zero collections and clears hidden collection scope from the request", async (t) => {
  const app = harness(t, { collections: [] }); await app.mapper();
  app.change("include-subcollections", true); app.scope("library");
  assert.equal(app.el("collection-fields").hidden, true);
  assert.equal(app.el("collection").required, false);
  assert.equal(app.el("estimate").disabled, false);
  await app.estimate();
  const body = app.requests.find((request) => request.url === "/api/estimate").body;
  assert.equal(body.scope, "library"); assert.equal(body.collection_key, ""); assert.equal(body.include_subcollections, false);
  assert.equal(app.el("review-collection").textContent, "Entire Zotero library");
  assert.match(app.el("cloud-consent-text").textContent, /Entire Zotero library/);
  app.click("approve"); assert.ok(!app.requests.some((request) => request.url === "/api/approve"));
});

test("nested collection choice and subcollection consent survive review and changes invalidate approval", async (t) => {
  const app = harness(t); await app.mapper();
  app.change("collection", "CHILD"); app.change("include-subcollections", true); await app.estimate();
  assert.match(app.el("review-collection").textContent, /Thesis \/ Reading \(including subcollections\)/);
  assert.match(app.el("cloud-consent-text").textContent, /Thesis \/ Reading and its subcollections/);
  const request = app.requests.find((request) => request.url === "/api/estimate").body;
  assert.equal(request.collection_key, "CHILD"); assert.equal(request.include_subcollections, true);
  app.change("cloud-consent", true); assert.equal(app.el("approve").disabled, false);
  app.scope("library");
  assert.equal(app.el("review").hidden, true); assert.equal(app.el("cloud-consent").checked, false);
  assert.ok(app.requests.some((request) => request.url === "/api/cancel"));
});

test("approved mapping hands off to the read-only Source Index and its atomic-note links", async (t) => {
  const app = harness(t); await app.mapper(); app.scope("library"); await app.estimate();
  app.change("cloud-consent", true); app.click("approve");
  await until(() => app.polls.has(2000), "mapping poll");
  await app.polls.get(2000)();
  assert.equal(app.el("run-title").textContent, "Your research memory is ready.");
  assert.equal(app.el("open-mapped-workspace").hidden, false);
  app.click("open-mapped-workspace");
  await until(() => app.el("workspace-breadcrumbs").title === master && !app.el("workspace-reader").hidden, "published source index");
  assert.equal(app.el("mapping-screen").hidden, true);
  assert.equal(app.el("workspace-save").disabled, true);
  await until(() => app.el("workspace-reader").querySelector("a.note-link"), "indexed navigation");
  app.el("workspace-reader").querySelector("a.note-link").click();
  await until(() => app.el("workspace-breadcrumbs").title === atomic, "atomic note");
  assert.equal(app.el("workspace-save").disabled, true);
  assert.ok(app.requests.some(({ url, body }) => url === "/api/approve" && body.allow_cloud === true));
});

test("switching to mapper preserves drafts, and cancelling the result handoff preserves the current workspace", async (t) => {
  const app = harness(t, { run: completed }); const view = await app.personal();
  view.dispatch({ changes: { from: view.state.doc.length, insert: "Unsaved thought.\n" } });
  await app.mapper(); app.click("open-mapped-workspace");
  await until(() => app.el("workspace-confirm-dialog").open, "draft guard");
  app.click("workspace-confirm-cancel"); app.click("show-workspace");
  assert.equal(app.el("workspace-breadcrumbs").title, "Draft.md");
  assert.match(view.state.doc.toString(), /Unsaved thought/);
  assert.equal(app.w.workspaceHasUnsavedChanges(), true);
  assert.deepEqual(app.requests.filter(({ url }) => url === "/api/workspace/open").map(({ body }) => body.path), ["/personal"]);
});

test("export failures retain the generated memory handoff while unavailable workspaces stay hidden", async (t) => {
  const app = harness(t, { run: { ...completed, status: "partial", export_error: "Export failed; the memory workspace is available." } });
  await app.mapper();
  assert.match(app.el("run-message").textContent, /Export failed/);
  assert.equal(app.el("open-mapped-workspace").hidden, false);
  const unavailable = harness(t, { run: { status: "failed", workspace: "/missing", workspace_available: false } });
  await unavailable.mapper();
  assert.equal(unavailable.el("open-mapped-workspace").hidden, true);
});
