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
  };
  window.fetch = async (url, request = {}) => {
    const body = request.body ? JSON.parse(request.body) : {};
    requests.push({ url, body });
    let status = 200, result;
    if (url === "/api/workspace/open" || url === "/api/workspace/tree") result = { workspace };
    else if (url === "/api/workspace/index") result = { documents: [{ ...data.disk }], relations: [], warnings: [], truncated: false };
    else if (url === "/api/workspace/read") result = { document: { ...data.disk } };
    else if (url === "/api/workspace/save") {
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
