import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { JSDOM, VirtualConsole } from "jsdom";

const asset = (name) => readFileSync(new URL(`../../src/auto_zettelkasten/web/${name}`, import.meta.url), "utf8");
const html = asset("index.html");
const scripts = ["workspace-vendor.js", "app.js", "workspace.js"].map(asset);

async function until(check, description) {
  const deadline = Date.now() + 2000;
  while (!check()) {
    if (Date.now() > deadline) assert.fail(`Timed out: ${description}`);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function harness(t, options = {}) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (error) => errors.push(error));
  const dom = new JSDOM(html, {
    url: "http://127.0.0.1:8765", runScripts: "dangerously",
    pretendToBeVisual: true, virtualConsole,
  });
  const window = dom.window;
  // jsdom has no layout or native dialog implementation.
  window.HTMLElement.prototype.scrollIntoView = function () {};
  window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  window.Range.prototype.getClientRects = () => [];
  window.Range.prototype.getBoundingClientRect = () => ({ left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 });

  const polls = [];
  const originalInterval = window.setInterval.bind(window);
  window.setInterval = (callback, delay) => {
    polls.push(callback);
    return originalInterval(callback, delay);
  };
  if (options.recovery) {
    for (const [key, value] of options.recovery) window.localStorage.setItem(key, value);
  }
  const requests = [];
  const source = options.text || "# Start\r\n\r\nOriginal **text**.\r\n";
  const data = {
    disk: {
      id: "note-1", path: "Start.md", name: "Start.md", text: source,
      revision: "original", editable: true, reason: "", bom: true,
      newline: source.includes("\r\n") ? "crlf" : "lf", recovery_key: "start-note",
      ...options.document,
    },
    saves: 0,
    beforeSave: null,
    afterSave: null,
  };
  const workspace = {
    id: "workspace-1", name: "Notes", path: "/notes", editable: true,
    entries: [{ path: "Start.md", name: "Start.md", kind: "file", editable: true }],
    truncated: false,
    ...options.workspace,
  };
  window.fetch = async (url, request = {}) => {
    const body = request.body ? JSON.parse(request.body) : {};
    requests.push({ url, body });
    let status = 200, result;
    if (url === "/api/workspace/open" || url === "/api/workspace/tree") result = { workspace };
    else if (url === "/api/workspace/index") result = { documents: [{ ...data.disk }], relations: [], warnings: [], truncated: false };
    else if (url === "/api/workspace/recall") {
      result = options.recall ? await options.recall(body) : { notes: [], provenance: { route: "full_note_lexical" }, provider_calls: 0 };
    }
    else if (url === "/api/workspace/recall-index") result = options.prepareRecall ? await options.prepareRecall(body) : { status: "prepared", provider_calls: 0 };
    else if (url === "/api/workspace/read") result = { document: { ...data.disk } };
    else if (url === "/api/workspace/save-personal-metadata") {
      if (options.metadataSave) result = await options.metadataSave(body);
      else {
        if (body.display_title !== undefined) data.disk.personal_display_title = body.display_title;
        if (body.personal_properties !== undefined) data.disk.personal_properties = body.personal_properties;
        data.disk.display_title = data.disk.personal_display_title || data.disk.name.replace(/\.md$/i, "");
        data.disk.revision = `metadata-${requests.length}`;
        result = { document: { ...data.disk } };
      }
    }
    else if (url === "/api/workspace/save" || url === "/api/workspace/save-override") {
      data.saves += 1;
      await data.beforeSave?.(body);
      if (body.revision !== data.disk.revision) {
        status = 409;
        result = { error: "The note changed on disk.", code: "conflict", current: { ...data.disk } };
      } else {
        data.disk = {
          ...data.disk,
          text: data.disk.newline === "crlf" ? body.text.replaceAll("\n", "\r\n") : body.text,
          revision: `saved-${data.saves}`,
          ...(url.endsWith("save-override") ? { override_saved: true } : {}),
        };
        data.afterSave?.(data.disk);
        result = { document: { ...data.disk } };
      }
    } else throw new Error(`Unexpected request: ${url}`);
    return { ok: status === 200, status, json: async () => structuredClone(result) };
  };
  for (const sourceText of scripts) {
    const script = window.document.createElement("script");
    script.textContent = sourceText;
    window.document.body.append(script);
  }
  t.after(() => {
    window.close();
    assert.deepEqual(errors, [], "No uncaught frontend or CodeMirror exceptions");
  });
  const element = (id) => window.document.getElementById(id);
  const click = (id) => element(id).click();
  const getView = () => window.WorkspaceKit.EditorView.findFromDOM(window.document.querySelector(".cm-editor"));
  const open = async () => {
    click("workspace-welcome-open");
    element("workspace-folder-path").value = "/notes";
    element("workspace-folder-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    await until(() => window.document.querySelector(".tree-row"), "folder tree");
    window.document.querySelector(".tree-row").click();
    await until(() => window.document.querySelector(".cm-editor"), "CodeMirror editor");
    return getView();
  };
  const append = (text) => {
    const view = getView();
    view.dispatch({ changes: { from: view.state.doc.length, insert: text }, userEvent: "input" });
  };
  const mode = (value) => window.document.querySelector(`[data-mode="${value}"]`).click();
  const save = async () => {
    click("workspace-save");
    await until(() => element("workspace-save-state").textContent !== "Saving…", "save outcome");
  };
  const confirm = async (accepted = true) => {
    await until(() => element("workspace-confirm-dialog").open, "app confirmation dialog");
    click(accepted ? "workspace-confirm-accept" : "workspace-confirm-cancel");
  };
  return { window, data, requests, element, click, open, append, mode, save, getView, confirm, poll: () => polls[0]() };
}

test("Personal source title saves explicitly, resets and preserves canonical body", async (t) => {
  const app = harness(t, { document: { editable: false, override_available: true, personal_properties: {}, display_title: "Start" } });
  const view = await app.open(); const original = view.state.doc.toString();
  for (const mode of ["source", "live", "reading"]) {
    app.mode(mode); app.click("workspace-file-title");
    app.element("workspace-title-input").value = "Unsaved title";
    app.element("workspace-title-input").dispatchEvent(new app.window.Event("input", { bubbles: true }));
    app.element("workspace-title-input").dispatchEvent(new app.window.FocusEvent("blur"));
    assert.equal(app.requests.filter((row) => row.url.endsWith("save-personal-metadata")).length, 0);
    app.element("workspace-title-input").dispatchEvent(new app.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    assert.equal(app.element("workspace-file-title").textContent, "Start");
  }
  app.click("workspace-file-title"); app.element("workspace-title-input").value = "My research title";
  app.element("workspace-title-form").dispatchEvent(new app.window.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => app.element("workspace-file-title").textContent === "My research title", "personal title save");
  assert.equal(app.data.disk.path, "Start.md"); assert.equal(app.getView().state.doc.toString(), original);
  assert.match(app.window.document.querySelector(".document-tab").textContent, /My research title/);
  app.click("workspace-file-title"); app.click("workspace-title-reset");
  await until(() => app.element("workspace-file-title").textContent === "Start", "reset title");
});

test("Unsaved body prevents metadata commits without losing either draft", async (t) => {
  const app = harness(t, { document: { editable: false, override_available: true } });
  await app.open(); app.append("\nMy pending body"); app.click("workspace-file-title");
  app.element("workspace-title-input").value = "Pending title";
  app.element("workspace-title-form").dispatchEvent(new app.window.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => app.element("workspace-title-error").textContent.includes("body changes"), "draft guard");
  assert.equal(app.element("workspace-title-input").value, "Pending title");
  assert.match(app.getView().state.doc.toString(), /My pending body/);
  assert.ok(!app.requests.some((row) => row.url.endsWith("save-personal-metadata")));
});

test("Failed metadata saves retain the title draft and close cancellation preserves it", async (t) => {
  const app = harness(t, { document: { editable: false, override_available: true }, metadataSave: () => { throw new Error("Offline test"); } });
  await app.open(); app.click("workspace-file-title"); app.element("workspace-title-input").value = "Keep my draft";
  app.element("workspace-title-form").dispatchEvent(new app.window.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => app.element("workspace-title-error").textContent.includes("Offline test"), "metadata failure");
  assert.equal(app.element("workspace-title-input").value, "Keep my draft");
  assert.equal(app.data.disk.path, "Start.md"); assert.equal(app.data.disk.personal_display_title, undefined);
  assert.equal(app.window.workspaceHasUnsavedChanges(), true);
  app.window.document.querySelector('[aria-label="Close Start.md"]').click();
  await app.confirm(false);
  assert.equal(app.element("workspace-title-input").value, "Keep my draft");
});

test("Typed personal-note properties preserve text and are undoable", async (t) => {
  const text = "---\n# Keep comment\ntags:\n- existing\nscore: 1 # Keep inline\n---\n# Body\nAuthored text.\n";
  const app = harness(t, { text }); await app.open();
  assert.ok(app.element("workspace-properties").textContent.includes("existing"));
  assert.ok(!app.window.document.querySelector(".cm-content").textContent.includes("Keep comment"));
  app.element("workspace-properties").querySelector('[aria-label="Edit property score"]').click();
  const form = app.element("workspace-properties").querySelector("form");
  form.querySelector("textarea").value = "2";
  form.dispatchEvent(new app.window.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => app.getView().state.doc.toString().includes("score: 2"), "property transaction");
  assert.match(app.getView().state.doc.toString(), /# Keep comment/);
  assert.match(app.getView().state.doc.toString(), /# Keep inline/);
  app.window.WorkspaceKit.undo(app.getView());
  assert.equal(app.getView().state.doc.toString(), text);
  app.mode("source"); assert.ok(app.element("workspace-properties").hidden);
  assert.ok(app.window.document.querySelector(".cm-content").textContent.includes("Keep comment"));
});

test("Generated properties are locked while personal additions use metadata endpoint", async (t) => {
  const app = harness(t, { text: "---\nsource_id: canonical\ntags:\n- generated\n---\n# Body\n", document: { editable: false, override_available: true, personal_properties: { tags: ["mine"] } } });
  await app.open();
  const panel = app.element("workspace-properties");
  assert.equal(panel.querySelector('[aria-label="Edit property source_id"]'), null);
  panel.querySelector('[aria-label="Edit property tags (personal)"]').click();
  const form = panel.querySelector("form"); form.querySelector("textarea").value = "mine\nresearch";
  form.dispatchEvent(new app.window.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => app.data.disk.personal_properties.tags.length === 2, "personal tags save");
  assert.deepEqual(app.data.disk.personal_properties.tags, ["mine", "research"]);
  assert.match(app.data.disk.text, /source_id: canonical\ntags:\n- generated/);
});

test("Workspace indicator explains portable exports and requires explicit source open", async (t) => {
  const app = harness(t, { workspace: { workspace_kind: "markdown_export", source_workspace: "/original/mapped" } });
  await app.open();
  assert.equal(app.element("workspace-kind-label").textContent, "Markdown export");
  assert.match(app.element("workspace-kind-description").textContent, /links but not saved communities/);
  assert.equal(app.element("workspace-open-mapped").title, "/original/mapped");
  assert.equal(app.requests.filter((row) => row.url.endsWith("workspace/open")).length, 1);
  app.click("workspace-open-mapped");
  await until(() => app.requests.some((row) => row.url.endsWith("workspace/open") && row.body.path === "/original/mapped"), "explicit mapped workspace action");
});

test("Legacy coverage inventories are compact in previews while source and citations remain intact", async (t) => {
  const inventory = "Recovered PDF pages: 1, 2, 4. Missing or unresolved PDF pages: 3. Claims in this note are restricted to the recovered pages and must not be treated as findings from the complete document.";
  const text = `# Start\n\n> [!warning] Coverage boundary\n> ${inventory}\n\n## Thesis\nA finding supported on PDF page 4.\n`;
  const app = harness(t, { text });
  const view = await app.open();
  await until(() => app.window.document.querySelector(".live-coverage-notice"), "compact live coverage notice");
  assert.ok(!app.window.document.querySelector(".cm-content").textContent.includes("Recovered PDF pages"));
  assert.equal(view.state.doc.toString(), text);
  app.mode("source");
  assert.ok(app.window.document.querySelector(".cm-content").textContent.includes("Recovered PDF pages"));
  app.mode("reading");
  const reading = app.element("workspace-reader");
  assert.ok(reading.textContent.includes("incomplete extraction"));
  assert.ok(!reading.textContent.includes("Recovered PDF pages"));
  assert.ok(reading.textContent.includes("PDF page 4"));
  assert.equal(app.data.saves, 0);
  assert.equal(app.data.disk.text, text);
});

test("quick-open finds full-note content, renders literal excerpts and opens its bound file", async (t) => {
  const app = harness(t, { recall: async (body) => {
    assert.equal(body.query, "participatory budgeting");
    assert.equal(body.max_notes, 8); assert.equal(body.max_chars, 12000);
    return { notes: [{ title: "A research method", note_path: "Start.md", text: "A participatory budgeting mechanism. <img src=x>" }],
      provenance: { route: "full_note_hybrid" }, provider_calls: 0 };
  } });
  await app.open(); app.click("workspace-quick-open");
  app.element("workspace-quick-query").value = "participatory budgeting";
  app.element("workspace-quick-query").dispatchEvent(new app.window.Event("input", { bubbles: true }));
  await until(() => app.element("workspace-quick-results").querySelector(".quick-excerpt"), "full-note search results");
  assert.match(app.element("workspace-quick-results").textContent, /participatory budgeting/);
  assert.equal(app.element("workspace-quick-results").querySelector("img"), null);
  assert.match(app.element("workspace-quick-status").textContent, /text and semantic search/);
  app.element("workspace-quick-query").dispatchEvent(new app.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  assert.equal(app.element("workspace-quick-dialog").open, false);
});

test("quick-open ignores older content responses and names lexical fallback honestly", async (t) => {
  const old = deferred();
  const app = harness(t, { recall: (body) => body.query === "older query" ? old.promise : Promise.resolve({
    notes: [{ title: "Current result", note_path: "Start.md", text: "Only the newest evidence." }],
    provenance: { route: "full_note_lexical" }, provider_calls: 0,
  }) });
  await app.open(); app.click("workspace-quick-open");
  const input = app.element("workspace-quick-query");
  const query = (value) => { input.value = value; input.dispatchEvent(new app.window.Event("input", { bubbles: true })); };
  query("older query");
  await until(() => app.requests.some(({ url, body }) => url.endsWith("/recall") && body.query === "older query"), "first content request");
  query("newer query");
  await until(() => app.element("workspace-quick-results").textContent.includes("Current result"), "newest result");
  old.resolve({ notes: [{ title: "Stale result", note_path: "Old.md", text: "Wrong query." }], provenance: { route: "full_note_hybrid" } });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.doesNotMatch(app.element("workspace-quick-results").textContent, /Stale result/);
  assert.match(app.element("workspace-quick-status").textContent, /Semantic search is unavailable/);
});

test("closing quick-open cancels queued content searches", async (t) => {
  const app = harness(t);
  await app.open(); app.click("workspace-quick-open");
  const input = app.element("workspace-quick-query"); input.value = "pending content";
  input.dispatchEvent(new app.window.Event("input", { bubbles: true }));
  app.element("workspace-quick-dialog").dispatchEvent(new app.window.Event("cancel", { cancelable: true }));
  await new Promise((resolve) => setTimeout(resolve, 220));
  assert.equal(app.requests.some(({ url }) => url.endsWith("/recall")), false);
});

test("partial semantic coverage is visible and local indexing is explicit", async (t) => {
  let indexed = false;
  const app = harness(t, { recall: async () => ({
    notes: [{ title: "A note", note_path: "Start.md", text: "Budgeting evidence." }],
    provenance: { route: "full_note_hybrid", vector_source_count: indexed ? 3 : 1, source_count: 3 },
  }), prepareRecall: async (body) => { assert.deepEqual(body, { workspace_id: "workspace-1" }); indexed = true; return { status: "prepared", provider_calls: 0 }; } });
  await app.open(); app.click("workspace-quick-open");
  const input = app.element("workspace-quick-query"); input.value = "budgeting";
  input.dispatchEvent(new app.window.Event("input", { bubbles: true }));
  await until(() => app.element("workspace-quick-status").textContent.includes("1 of 3"), "partial semantic coverage");
  assert.equal(app.requests.some(({ url }) => url.endsWith("/recall-index")), false);
  assert.equal(app.element("workspace-quick-index").hidden, false);
  app.click("workspace-quick-index");
  await until(() => app.element("workspace-quick-status").textContent === "Local text and semantic search.", "index refresh");
  assert.equal(app.element("workspace-quick-index").hidden, true);
});

test("workspace opens offline and preserves Markdown, history and CRLF save conventions across modes", async (t) => {
  const app = harness(t);
  assert.equal(app.requests.length, 0, "Opening the app must not contact Zotero or a provider");
  const view = await app.open();
  assert.equal(view.state.doc.toString(), "# Start\n\nOriginal **text**.\n");
  assert.equal(app.window.workspaceHasUnsavedChanges(), false);
  const original = view.state.doc.toString();
  app.append("Added locally.\n");
  assert.equal(app.window.workspaceHasUnsavedChanges(), true);
  app.mode("reading");
  assert.match(app.element("workspace-reader").textContent, /Added locally/);
  app.mode("source");
  assert.equal(app.window.WorkspaceKit.undo(view), true);
  assert.equal(view.state.doc.toString(), original);
  assert.equal(app.window.WorkspaceKit.redo(view), true);
  app.mode("live");
  await app.save();
  assert.equal(app.window.workspaceHasUnsavedChanges(), false);
  assert.match(app.data.disk.text, /Added locally\.\r\n$/);
  assert.equal(view.state.doc.toString().includes("\r"), false);
  assert.ok(app.requests.every(({ url }) => url.startsWith("/api/workspace/")));
});

test("typing while a save is pending keeps the newer draft dirty", async (t) => {
  const app = harness(t, { text: "Original\n" });
  const view = await app.open();
  const pending = deferred();
  app.data.beforeSave = () => pending.promise;
  app.append("First change\n");
  app.click("workspace-save");
  await until(() => app.data.saves === 1, "save request");
  app.append("Newer change\n");
  pending.resolve();
  await until(() => app.element("workspace-save-state").textContent !== "Saving…", "save completion");
  assert.equal(app.data.disk.text, "Original\nFirst change\n");
  assert.equal(view.state.doc.toString(), "Original\nFirst change\nNewer change\n");
  assert.equal(app.window.workspaceHasUnsavedChanges(), true);
  assert.equal(app.element("workspace-save").disabled, false);
});

test("conflict review keeps both versions and saves the edited draft with reviewed CAS revision", async (t) => {
  const app = harness(t, { text: "Original\n" });
  const view = await app.open();
  app.append("My draft\n");
  app.data.disk = { ...app.data.disk, text: "External version\n", revision: "external-1" };
  await app.save();
  assert.equal(app.element("workspace-conflict").hidden, false);
  assert.equal(view.state.doc.toString(), "Original\nMy draft\n");
  app.click("workspace-review-disk");
  assert.equal(app.element("workspace-draft-text").textContent, "Original\nMy draft\n");
  assert.equal(app.element("workspace-disk-text").textContent, "External version\n");

  // A second writer changes the file after review; the confirmed save must still fail safely.
  app.data.disk = { ...app.data.disk, text: "Newer external version\n", revision: "external-2" };
  app.click("workspace-save-draft");
  await app.confirm();
  await until(() => app.data.saves === 2 && app.element("workspace-save-state").textContent !== "Saving…", "second conflict");
  assert.equal(app.data.disk.text, "Newer external version\n");
  assert.equal(view.state.doc.toString(), "Original\nMy draft\n");
  app.click("workspace-review-disk");
  assert.equal(app.element("workspace-disk-text").textContent, "Newer external version\n");
  app.click("workspace-save-draft");
  await app.confirm();
  await until(() => app.data.saves === 3 && app.element("workspace-save-state").textContent !== "Saving…", "reviewed save");
  assert.equal(app.requests.filter(({ url }) => url.endsWith("/save")).at(-1).body.revision, "external-2");
  assert.equal(app.data.disk.text, "Original\nMy draft\n");
  assert.equal(app.window.workspaceHasUnsavedChanges(), false);
  assert.equal(app.element("workspace-disk-panel").hidden, true);
});

test("save responses and same-revision polls update edit policy without losing undo history", async (t) => {
  const app = harness(t, { text: "Original\n" });
  const view = await app.open();
  app.append("Local change\n");
  const text = view.state.doc.toString(), revision = app.data.disk.revision;
  app.data.disk = { ...app.data.disk, editable: false, reason: "Generated content is managed by the engine." };
  await app.poll();
  assert.equal(app.data.disk.revision, revision);
  assert.equal(view.state.readOnly, true);
  assert.equal(view.state.doc.toString(), text);
  assert.equal(app.element("workspace-policy").hidden, false);
  assert.equal(app.element("workspace-save").disabled, true);
  app.data.disk = { ...app.data.disk, editable: true, reason: "" };
  await app.poll();
  assert.equal(view.state.readOnly, false);
  assert.equal(app.window.WorkspaceKit.undo(view), true);
  assert.equal(view.state.doc.toString(), "Original\n");
  assert.equal(app.window.WorkspaceKit.redo(view), true);
  app.data.afterSave = (disk) => { disk.editable = false; disk.reason = "Engine-owned metadata."; };
  await app.save();
  assert.equal(view.state.readOnly, true);
  assert.equal(app.element("workspace-policy").textContent, "Engine-owned metadata.");
  assert.equal(app.window.workspaceHasUnsavedChanges(), false);
});

test("a reload recovery offers restore and retains the newer disk version for review", async (t) => {
  const first = harness(t, { text: "Original\n" });
  await first.open();
  first.append("Recovered draft\n");
  const recovery = [];
  for (let index = 0; index < first.window.localStorage.length; index += 1) {
    const key = first.window.localStorage.key(index);
    recovery.push([key, first.window.localStorage.getItem(key)]);
  }
  assert.equal(recovery.length, 1);
  const reloaded = harness(t, { text: "External version\n", document: { revision: "new-disk" }, recovery });
  const view = await reloaded.open();
  assert.equal(reloaded.element("workspace-recovery").hidden, false);
  assert.equal(view.state.readOnly, true, "Recovery choice precedes editing");
  reloaded.click("workspace-restore");
  assert.equal(reloaded.getView().state.doc.toString(), "Original\nRecovered draft\n");
  assert.equal(reloaded.getView().state.readOnly, false);
  assert.equal(reloaded.element("workspace-conflict").hidden, false);
  reloaded.click("workspace-review-disk");
  assert.equal(reloaded.element("workspace-disk-text").textContent, "External version\n");
  assert.equal(reloaded.data.disk.text, "External version\n");
  assert.equal(reloaded.window.workspaceHasUnsavedChanges(), true);
});

test("a draft matching a stale conflict snapshot still requires CAS before clearing recovery", async (t) => {
  const app = harness(t, { text: "A\n" });
  const view = await app.open();
  view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: "B\n" }, userEvent: "input" });
  app.data.disk = { ...app.data.disk, text: "B\n", revision: "snapshot-B" };
  await app.save();
  assert.equal(app.element("workspace-conflict").hidden, false);
  app.click("workspace-review-disk");
  assert.equal(app.element("workspace-disk-text").textContent, "B\n");
  app.data.disk = { ...app.data.disk, text: "C\n", revision: "newer-C" };
  app.click("workspace-save-draft");
  await app.confirm();
  await until(() => app.data.saves === 2 && app.element("workspace-save-state").textContent !== "Saving…", "stale identical-text conflict");
  assert.equal(app.requests.filter(({ url }) => url.endsWith("/save")).at(-1).body.revision, "snapshot-B");
  assert.equal(app.data.disk.text, "C\n");
  assert.equal(view.state.doc.toString(), "B\n");
  assert.equal(app.window.workspaceHasUnsavedChanges(), true);
  assert.ok(app.window.localStorage.length > 0, "Unsaved draft recovery stays available");
  await app.poll();
  assert.equal(view.state.doc.toString(), "B\n", "Polling cannot replace the preserved conflict draft");
});

test("app confirmation cancellation preserves dirty tabs and root; disk replacement rechecks the reviewed version", async (t) => {
  const app = harness(t, { text: "Original\n" });
  const view = await app.open();
  app.append("Local draft\n");
  const close = app.window.document.querySelector('[aria-label="Close Start.md"]');
  close.focus(); close.click();
  await app.confirm(false);
  assert.equal(app.getView(), view);
  assert.match(view.state.doc.toString(), /Local draft/);
  assert.equal(app.window.document.activeElement, close, "Cancel restores focus to the initiating control");

  app.click("workspace-root");
  app.element("workspace-folder-path").value = "/other-notes";
  app.element("workspace-folder-form").dispatchEvent(new app.window.Event("submit", { bubbles: true, cancelable: true }));
  await app.confirm(false);
  assert.equal(app.requests.filter(({ url }) => url === "/api/workspace/open").length, 1);
  assert.match(view.state.doc.toString(), /Local draft/);
  app.window.document.querySelector('[data-close-dialog="workspace-folder-dialog"]').click();

  app.data.disk = { ...app.data.disk, text: "External A\n", revision: "external-A" };
  await app.save();
  app.click("workspace-use-disk");
  await until(() => app.element("workspace-confirm-dialog").open, "disk replacement confirmation");
  app.data.disk = { ...app.data.disk, text: "External B\n", revision: "external-B" };
  await app.poll();
  await app.confirm();
  await until(() => !app.element("workspace-confirm-dialog").open, "confirmation dismissed");
  assert.match(view.state.doc.toString(), /Local draft/, "A changed disk snapshot cannot discard the draft");
  app.click("workspace-use-disk");
  await app.confirm();
  await until(() => view.state.doc.toString() === "External B\n", "explicit disk replacement");
  assert.equal(app.window.workspaceHasUnsavedChanges(), false);
});

test("discarding a recovered draft uses the app dialog and only changes it after acceptance", async (t) => {
  const first = harness(t, { text: "Original\n" });
  await first.open(); first.append("Recovered draft\n");
  const key = first.window.localStorage.key(0);
  const reloaded = harness(t, { text: "Original\n", recovery: [[key, first.window.localStorage.getItem(key)]] });
  await reloaded.open();
  reloaded.click("workspace-discard-recovery");
  await reloaded.confirm(false);
  assert.equal(reloaded.element("workspace-recovery").hidden, false);
  assert.equal(reloaded.getView().state.readOnly, true);
  reloaded.click("workspace-discard-recovery");
  await reloaded.confirm();
  await until(() => reloaded.element("workspace-recovery").hidden, "recovery discarded");
  assert.equal(reloaded.getView().state.doc.toString(), "Original\n");
  assert.equal(reloaded.getView().state.readOnly, false);
  assert.equal(reloaded.window.workspaceHasUnsavedChanges(), false);
  assert.equal(reloaded.window.localStorage.length, 0);
});


test("Generated notes edit and save personal overrides in Source and Live Preview while reading stays a preview", async (t) => {
  const app = harness(t, { text: "# Generated\n\nOriginal body.\n", document: { editable: false, override_available: true, reason: "Generated note is protected." } });
  const view = await app.open();
  assert.equal(view.state.readOnly, false);
  assert.equal(app.window.document.querySelector('[data-mode="live"]').getAttribute("aria-pressed"), "true");
  assert.match(app.element("workspace-policy").textContent, /saved privately/);
  app.mode("source"); app.append("Personal addition.\n"); await app.save();
  assert.equal(app.data.disk.editable, false, "Canonical ownership must remain protected");
  assert.equal(app.requests.filter(({ url }) => url === "/api/workspace/save").length, 0);
  assert.equal(app.requests.filter(({ url }) => url === "/api/workspace/save-override").length, 1);
  assert.equal(app.element("workspace-save-state").textContent, "Personal edit saved");
  app.mode("reading"); assert.match(app.element("workspace-reader").textContent, /Personal addition/);
  assert.match(app.element("workspace-policy").textContent, /Choose Source or Live Preview to edit/);
  app.mode("live"); app.append("Another addition.\n"); await app.save();
  assert.equal(app.window.workspaceHasUnsavedChanges(), false);
  assert.equal(app.getView().state.readOnly, false);
});

test("Override conflicts keep drafts writable and allow the existing reviewed save flow", async (t) => {
  const app = harness(t, { text: "Generated body.\n", document: { editable: false, override_available: true } });
  await app.open(); app.append("Personal draft.\n");
  app.data.beforeSave = () => { app.data.beforeSave = null; app.data.disk.revision = "other-override"; app.data.disk.text = "Other personal edit.\n"; app.data.disk.override_saved = true; };
  await app.save();
  assert.equal(app.element("workspace-conflict").hidden, false);
  assert.equal(app.getView().state.readOnly, false);
  app.click("workspace-review-disk");
  assert.equal(app.element("workspace-save-draft").disabled, false);
  app.click("workspace-save-draft"); await app.confirm();
  await until(() => app.element("workspace-conflict").hidden, "reviewed personal override save");
  assert.match(app.data.disk.text, /Personal draft/);
  assert.equal(app.requests.filter(({ url }) => url === "/api/workspace/save").length, 0);
});

test("Reviewed override saves retain regenerated properties while preserving the unsaved body", async (t) => {
  const header = "---\nnote_id: atomic-one\nengine_version: old\n---\n";
  const latest = header.replace("old", "new");
  const app = harness(t, { text: header + "Generated body.\n", document: { editable: false, override_available: true } });
  await app.open(); app.append("Personal draft.\n");
  app.data.beforeSave = () => { app.data.beforeSave = null; app.data.disk.revision = "regenerated"; app.data.disk.text = latest + "New generated body.\n"; app.data.disk.source_changed = true; };
  await app.save(); app.click("workspace-review-disk"); app.click("workspace-save-draft"); await app.confirm();
  await until(() => app.element("workspace-conflict").hidden, "rebased personal override save");
  assert.ok(app.data.disk.text.startsWith(latest)); assert.match(app.data.disk.text, /Personal draft/);
  assert.equal(app.getView().state.doc.toString(), app.data.disk.text);
  assert.equal(app.data.disk.editable, false);
});
