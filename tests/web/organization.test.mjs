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

const initialNotes = {
  "One.md": "# Source one\n\n[[Target Name]] [[Target Name|Alias text]]\n\n`[[Target Name]]`\n",
  "Two.md": '# Source two\n\n[[Notes/Target Name#Section]]\n\n[Encoded](Notes/Target%20Name.md#Section "A title")\n\n[[Target Name#^block]]\n\n![[Target Name#Section]]\n\n[Angle](<Notes/Target Name.md#Section>)\n',
  "Notes/Target Name.md": "---\naliases: [Target alias]\n---\n# Authored title\n\n## Section\n\nParagraph ^block\n",
};

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
  const requests = [], polls = [], hooks = {}, graphs = [];
  const notes = new Map(), directories = new Set(["Notes", "Drafts", "Archive"]);
  let revision = 0, tokenCounter = 0;
  const recovery = (path) => `note:${path}`;
  const put = (path, text, extra = {}) => {
    const previous = notes.get(path);
    const note = { id: previous?.id || `id:${path}`, path, name: path.split("/").at(-1), text, revision: `v${++revision}`, editable: true, reason: "", bom: false, newline: "lf", recovery_key: recovery(path), ...extra };
    notes.set(path, note); return note;
  };
  for (const [path, text] of Object.entries(options.texts || initialNotes)) put(path, text);
  if (options.protectedPath) notes.get(options.protectedPath).editable = false;
  const workspace = () => ({ id: "root", name: "Notes", path: "/notes", editable: true, truncated: false, entries: [
    ...[...directories].map((path) => ({ path, name: path.split("/").at(-1), kind: "directory", editable: true })),
    ...[...notes.values()].map((note) => ({ path: note.path, name: note.name, kind: "file", editable: note.editable, reason: note.reason })),
  ] });
  const snapshots = new Map();
  const originalInterval = w.setInterval.bind(w);
  w.setInterval = (callback, delay) => { polls.push(callback); return originalInterval(callback, delay); };
  w.fetch = async (url, request) => {
    const body = JSON.parse(request.body || "{}"); requests.push({ url, body });
    let result, status = 200;
    if (url.endsWith("/open") || url.endsWith("/tree")) result = { workspace: workspace() };
    else if (url.endsWith("/index")) result = { documents: [...notes.values()], relations: [], warnings: [], truncated: false };
    else if (url.endsWith("/read")) {
      const note = notes.get(body.path); assert.ok(note, `Readable synthetic note: ${body.path}`);
      result = { document: { ...note } };
    } else if (url.endsWith("/save")) {
      const note = [...notes.values()].find((value) => value.id === body.document_id);
      assert.ok(note, "Save adopts the current server document ID");
      assert.equal(body.revision, note.revision);
      result = { document: put(note.path, body.text, { id: note.id }) };
    } else if (url.endsWith("/relocate-preview")) {
      const kind = directories.has(body.source) ? "directory" : "file";
      const mapping = (path) => path === body.source ? body.destination : kind === "directory" && path.startsWith(`${body.source}/`) ? body.destination + path.slice(body.source.length) : path;
      const moves = [...notes.values()].filter((note) => mapping(note.path) !== note.path).map((note) => ({ from: note.path, to: mapping(note.path), recovery_key: recovery(note.path), target_recovery_key: recovery(mapping(note.path)) }));
      const preview = { token: `token-${++tokenCounter}`, source: body.source, destination: body.destination, kind, moves, documents: structuredClone([...notes.values()]), warnings: [], truncated: Boolean(options.partial) };
      snapshots.set(preview.token, preview); result = preview;
      await hooks.preview?.(preview);
    } else if (url.endsWith("/relocate")) {
      const preview = snapshots.get(body.token); assert.ok(preview, "A reviewed token is required");
      await hooks.apply?.(preview);
      const stale = preview.documents.some((note) => notes.get(note.path)?.revision !== note.revision);
      if (stale) { status = 409; result = { error: "The preview expired because files changed." }; }
      else {
        const updated = new Set(), moved = new Map(preview.moves.map((move) => [move.from, move.to]));
        if (body.repair) for (const patch of body.patches) {
          const note = notes.get(patch.path); assert.equal(note.revision, patch.revision);
          let text = note.text.replaceAll("\r\n", "\n");
          for (const edit of patch.edits) { assert.equal(text.slice(edit.from, edit.to), edit.before); text = text.slice(0, edit.from) + edit.after + text.slice(edit.to); }
          put(note.path, text); updated.add(note.path);
        }
        for (const [from, to] of moved) {
          const note = notes.get(from); notes.delete(from);
          put(to, note.text, { id: `moved:${to}` }); updated.delete(from); updated.add(to);
        }
        if (preview.kind === "directory") {
          for (const path of [...directories]) if (path === preview.source || path.startsWith(`${preview.source}/`)) { directories.delete(path); directories.add(preview.destination + path.slice(preview.source.length)); }
        }
        result = { workspace: workspace(), moves: preview.moves.map(({ from, to }) => ({ from, to })), documents: [...updated].map((path) => notes.get(path)) };
        await hooks.receipt?.(result);
      }
    } else throw new Error(`Unexpected request ${url}`);
    return { ok: status === 200, status, json: async () => structuredClone(result) };
  };
  const load = (name) => { const script = w.document.createElement("script"); script.textContent = asset(name); w.document.body.append(script); };
  load("workspace-vendor.js");
  const kit = w.WorkspaceKit;
  w.WorkspaceKit = { ...kit, cytoscape: (options) => { const config = w.Object.assign(new w.Object(), options); config.container = undefined; config.headless = true; config.styleEnabled = true; const graph = kit.cytoscape(config); graphs.push(graph); return graph; } };
  load("app.js"); load("workspace.js");
  const el = (id) => w.document.getElementById(id), click = (id) => el(id).click();
  const input = (id, text) => { el(id).value = text; el(id).dispatchEvent(new w.Event("input", { bubbles: true })); };
  const view = () => kit.EditorView.findFromDOM(w.document.querySelector(".cm-editor"));
  const root = async () => {
    click("workspace-welcome-open"); input("workspace-folder-path", "/notes");
    el("workspace-folder-form").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
    await until(() => requests.some(({ url }) => url.endsWith("/index")), "workspace index");
    await until(() => el("workspace-index-status").hidden, "workspace index complete");
  };
  const open = async (path) => {
    click("workspace-quick-open"); input("workspace-quick-query", path);
    [...el("workspace-quick-results").querySelectorAll("button")].find((button) => button.querySelector("small").textContent === path)?.click();
    await until(() => el("workspace-breadcrumbs").title === path && !el("workspace-document-header").hidden, `open ${path}`);
    return view();
  };
  const treeRow = (path) => [...el("workspace-tree").querySelectorAll(".tree-row")].find((row) => row.dataset.path === path);
  const focus = (path) => {
    let parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
    const parents = [];
    while (parent) { parents.unshift(parent); parent = parent.includes("/") ? parent.slice(0, parent.lastIndexOf("/")) : ""; }
    for (const folder of parents) { const row = treeRow(folder); if (row?.getAttribute("aria-expanded") === "false") row.click(); }
    const row = treeRow(path); assert.ok(row, `Tree row: ${path}`); row.focus(); return row;
  };
  const rename = async (path, name, method = "keyboard") => {
    const row = focus(path);
    if (method === "keyboard") row.dispatchEvent(new w.KeyboardEvent("keydown", { key: "F2", bubbles: true }));
    else { click("workspace-file-actions"); click("workspace-file-rename"); }
    await until(() => el("workspace-organization-dialog").open, "rename dialog");
    input("workspace-rename-name", name);
    el("workspace-organization-form").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
    await until(() => el("workspace-organization-review").open || el("workspace-organization-error").textContent && !el("workspace-organization-error").textContent.includes("Reviewing"), "rename review");
  };
  const confirm = async () => { await until(() => el("workspace-confirm-dialog").open, "confirmation"); click("workspace-confirm-accept"); };
  t.after(() => { for (const graph of graphs) graph.destroy(); w.close(); assert.deepEqual(errors, []); });
  return { w, kit, el, click, input, view, notes, put, requests, hooks, root, open, focus, rename, confirm, poll: () => polls[0]() };
}

test("rename review counts seven references in two files, shows exact destination changes, and Cancel makes no writes", async (t) => {
  const app = harness(t); await app.root(); await app.open("Notes/Target Name.md");
  const before = new Map([...app.notes].map(([path, note]) => [path, note.text]));
  await app.rename("Notes/Target Name.md", "Renamed Target.md");
  assert.equal(app.el("workspace-organization-counts").textContent, "7 references in 2 files to update.");
  assert.equal(app.el("workspace-organization-from").textContent, "Notes/Target Name.md");
  assert.equal(app.el("workspace-organization-to").textContent, "Notes/Renamed Target.md");
  assert.match(app.el("workspace-organization-edits").textContent, /Target Name.*Renamed%20Target/s);
  assert.equal(app.w.workspaceHasUnsavedChanges(), true, "In-progress organization guards native close");
  app.click("workspace-organization-review-cancel");
  assert.deepEqual(new Map([...app.notes].map(([path, note]) => [path, note.text])), before);
  assert.equal(app.requests.filter(({ url }) => url.endsWith("/relocate")).length, 0);
  assert.equal(app.w.workspaceHasUnsavedChanges(), false);
});

test("declining link updates preserves authored text and migrates the active tab, selection, undo and backend identity", async (t) => {
  const app = harness(t); await app.root(); const view = await app.open("Notes/Target Name.md");
  app.w.document.querySelector('[data-mode="source"]').click();
  view.dispatch({ changes: { from: view.state.doc.length, insert: "Saved annotation.\n" } });
  app.click("workspace-save"); await until(() => app.el("workspace-save-state").textContent === "Saved", "saved annotation");
  view.dispatch({ selection: { anchor: 5, head: 15 } });
  const originalText = view.state.doc.toString(), originalSource = app.notes.get("One.md").text;
  await app.rename("Notes/Target Name.md", "Renamed Target.md", "toolbar");
  app.click("workspace-organization-decline");
  await until(() => !app.el("workspace-organization-review").open, "rename complete");
  assert.equal(app.el("workspace-breadcrumbs").title, "Notes/Renamed Target.md");
  assert.equal(app.view(), view);
  assert.equal(view.state.doc.toString(), originalText);
  assert.equal(view.state.selection.main.anchor, 5); assert.equal(view.state.selection.main.head, 15);
  assert.equal(app.w.document.querySelector('[data-mode="source"]').getAttribute("aria-pressed"), "true");
  assert.equal(app.notes.get("One.md").text, originalSource);
  assert.equal(app.requests.filter(({ url }) => url.endsWith("/relocate")).at(-1).body.repair, false);
  assert.match(app.notes.get("Notes/Renamed Target.md").text, /# Authored title/);
  assert.equal(app.kit.undo(view), true, "Unchanged moved text retains its editing history");
  app.click("workspace-save"); await until(() => app.el("workspace-save-state").textContent === "Saved", "save after rename");
  assert.equal(app.requests.filter(({ url }) => url.endsWith("/save")).at(-1).body.document_id, "moved:Notes/Renamed Target.md");
});

test("reviewed repair updates source tabs, links, quick-open and backlinks while leaving target prose unchanged", async (t) => {
  const app = harness(t); await app.root(); await app.open("One.md"); await app.open("Notes/Target Name.md");
  const targetText = app.notes.get("Notes/Target Name.md").text;
  await app.open("One.md");
  const sourceView = app.view();
  sourceView.dispatch({ changes: { from: sourceView.state.doc.length, insert: "Saved annotation.\n" } });
  app.click("workspace-save"); await until(() => app.el("workspace-save-state").textContent === "Saved", "source annotation saved");
  await app.rename("Notes/Target Name.md", "Renamed Target.md");
  app.click("workspace-organization-repair");
  await until(() => !app.el("workspace-organization-review").open, "repair applied");
  assert.equal(app.notes.get("Notes/Renamed Target.md").text, targetText);
  assert.match(app.notes.get("One.md").text, /\[\[Renamed%20Target\|Alias text\]\]/);
  assert.match(app.notes.get("One.md").text, /`\[\[Target Name\]\]`/);
  assert.match(app.notes.get("Two.md").text, /Notes\/Renamed%20Target\.md#Section/);
  assert.equal(app.view(), sourceView);
  assert.match(sourceView.state.doc.toString(), /Renamed%20Target/);
  assert.match(sourceView.state.doc.toString(), /Saved annotation/);
  assert.equal(app.kit.undo(sourceView), false, "Reviewed source repairs restart undo history");
  await app.open("Notes/Renamed Target.md");
  app.click("workspace-backlinks-toggle");
  assert.equal(app.el("workspace-backlinks-title").textContent, "Linked mentions · 7");
  await app.open("One.md");
  assert.match(app.view().state.doc.toString(), /Renamed%20Target/);
  app.click("workspace-quick-open"); app.input("workspace-quick-query", "Target alias");
  assert.match(app.el("workspace-quick-results").textContent, /Notes\/Renamed Target.md/);
});

test("organization blocks dirty and closed recovery drafts, freezes typing/navigation, and rejects a stale apply", async (t) => {
  const app = harness(t); await app.root(); const view = await app.open("Notes/Target Name.md");
  view.dispatch({ changes: { from: view.state.doc.length, insert: "Unsaved draft\n" } });
  app.focus("Notes/Target Name.md").dispatchEvent(new app.w.KeyboardEvent("keydown", { key: "F2", bubbles: true }));
  assert.equal(app.el("workspace-organization-dialog").open, false);
  assert.match(app.el("workspace-message").textContent, /Save or review all drafts/);
  const close = app.w.document.querySelector('[aria-label="Close Target Name.md"]'); close.click(); await app.confirm();
  await until(() => app.el("workspace-document-header").hidden, "dirty tab closed");
  await app.rename("Notes/Target Name.md", "Renamed Target.md");
  assert.equal(app.el("workspace-organization-review").open, false);
  assert.match(app.el("workspace-organization-error").textContent, /Review recovery drafts/);
  assert.equal(app.notes.has("Notes/Target Name.md"), true);
  app.click("workspace-organization-cancel");
  await app.open("Notes/Target Name.md"); app.click("workspace-discard-recovery"); await app.confirm();
  await until(() => app.el("workspace-recovery").hidden, "draft explicitly discarded");
  await app.rename("Notes/Target Name.md", "Renamed Target.md");
  const currentView = app.view(), text = currentView.state.doc.toString(), requests = app.requests.length;
  currentView.dispatch({ changes: { from: currentView.state.doc.length, insert: "Typing during review" } });
  assert.equal(currentView.state.doc.toString(), text);
  app.focus("One.md").click(); assert.equal(app.el("workspace-breadcrumbs").title, "Notes/Target Name.md");
  await app.poll(); assert.equal(app.requests.length, requests, "Polling pauses while review is open");
  app.put("One.md", "An external change invalidates the snapshot.\n");
  app.click("workspace-organization-repair");
  await until(() => /preview expired/.test(app.el("workspace-organization-review-error").textContent), "stale apply rejected");
  assert.equal(app.notes.has("Notes/Target Name.md"), true);
  assert.equal(app.notes.has("Notes/Renamed Target.md"), false);
  assert.equal(currentView.state.doc.toString(), text);
  assert.equal(app.el("workspace-organization-repair").disabled, true);
  app.click("workspace-organization-review-cancel");
});

test("move folder picker excludes descendants and migrates folder and note identities without requiring a link rewrite", async (t) => {
  const app = harness(t); await app.root(); await app.open("Notes/Target Name.md");
  app.click("workspace-local-toggle"); app.click("local-graph-pin");
  app.el("local-graph-folder").value = "Notes";
  app.focus("Notes").dispatchEvent(new app.w.KeyboardEvent("keydown", { key: "F10", shiftKey: true, bubbles: true }));
  app.click("workspace-file-move");
  assert.equal(app.el("workspace-organization-dialog").open, true);
  app.input("workspace-move-search", "Drafts");
  const folder = app.el("workspace-move-folders").querySelector('[role="option"]'); assert.ok(folder); folder.click();
  app.el("workspace-organization-form").dispatchEvent(new app.w.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => app.el("workspace-organization-review").open, "folder move review");
  assert.equal(app.el("workspace-organization-to").textContent, "Drafts/Notes");
  app.click("workspace-organization-decline"); await until(() => !app.el("workspace-organization-review").open, "folder moved");
  assert.equal(app.el("workspace-breadcrumbs").title, "Drafts/Notes/Target Name.md");
  assert.equal(app.notes.has("Drafts/Notes/Target Name.md"), true);
  assert.equal(app.el("local-graph-folder").value, "Drafts/Notes");
  assert.equal(app.el("local-graph-pin").getAttribute("aria-pressed"), "true");
  assert.equal(app.el("local-graph-title").textContent, "Graph of Target Name");
  assert.ok(app.focus("Drafts/Notes/Target Name.md"));
});

test("generated references disable repair, and generated source entries have explained disabled actions", async (t) => {
  const app = harness(t, { protectedPath: "One.md" }); await app.root(); await app.open("Notes/Target Name.md");
  await app.rename("Notes/Target Name.md", "Renamed Target.md");
  assert.equal(app.el("workspace-organization-repair").disabled, true);
  assert.equal(app.el("workspace-organization-decline").disabled, false);
  assert.match(app.el("workspace-organization-warnings").textContent, /One.md.*protected/);
  app.click("workspace-organization-review-cancel");
  app.focus("One.md").dispatchEvent(new app.w.KeyboardEvent("contextmenu", { bubbles: true }));
  // Toolbar is also available when a native context menu is unavailable.
  app.click("workspace-file-actions");
  assert.equal(app.el("workspace-file-rename").disabled, true);
  assert.equal(app.el("workspace-file-move").disabled, true);
  assert.match(app.el("workspace-file-menu-reason").textContent, /Generated content/);
});

test("partial previews and pre-existing destination recovery drafts fail closed", async (t) => {
  const partial = harness(t, { partial: true }); await partial.root(); await partial.open("Notes/Target Name.md");
  await partial.rename("Notes/Target Name.md", "Renamed Target.md");
  assert.equal(partial.el("workspace-organization-review").open, false);
  assert.match(partial.el("workspace-organization-error").textContent, /preview is partial/);
  assert.equal(partial.requests.some(({ url }) => url.endsWith("/relocate")), false);

  const draft = harness(t); await draft.root(); await draft.open("Notes/Target Name.md");
  draft.w.localStorage.setItem("az.workspace.draft.note:Notes/Renamed Target.md", JSON.stringify({ text: "Unrelated recovery draft", revision: "old" }));
  await draft.rename("Notes/Target Name.md", "Renamed Target.md");
  assert.equal(draft.el("workspace-organization-review").open, false);
  assert.match(draft.el("workspace-organization-error").textContent, /Review recovery drafts.*Renamed Target/);
  assert.equal(draft.w.localStorage.length, 1);
});

test("an apply response lost after commit keeps the old view read-only until the folder is reopened", async (t) => {
  const app = harness(t); await app.root(); const view = await app.open("Notes/Target Name.md");
  await app.rename("Notes/Target Name.md", "Renamed Target.md");
  app.hooks.receipt = () => { throw new Error("Connection lost after the file operation."); };
  app.click("workspace-organization-repair");
  await until(() => /operation result could not be verified/.test(app.el("workspace-organization-review-error").textContent), "ambiguous apply outcome");
  assert.equal(app.notes.has("Notes/Renamed Target.md"), true, "The server already committed");
  app.click("workspace-organization-review-cancel");
  assert.equal(view.state.readOnly, true);
  assert.equal(app.el("workspace-save").disabled, true);
  assert.equal(app.el("workspace-save-state").textContent, "Reopen required");
  const original = view.state.doc.toString(), calls = app.requests.length;
  view.dispatch({ changes: { from: 0, insert: "Cannot edit an unverified view" } });
  app.click("workspace-save"); await app.poll();
  assert.equal(view.state.doc.toString(), original);
  assert.equal(app.requests.length, calls);
  app.click("workspace-root"); app.input("workspace-folder-path", "/notes");
  app.el("workspace-folder-form").dispatchEvent(new app.w.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => app.el("workspace-document-header").hidden, "folder reopened");
  await app.open("Notes/Renamed Target.md");
  assert.equal(app.view().state.readOnly, false);
  assert.equal(app.el("workspace-save-state").textContent, "Saved");
});
