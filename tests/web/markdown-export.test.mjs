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

function harness(t, options = {}) {
  const errors = [], console = new VirtualConsole();
  console.on("jsdomError", (error) => errors.push(error));
  const dom = new JSDOM(asset("index.html"), { url: "http://127.0.0.1:8765", runScripts: "dangerously", pretendToBeVisual: true, virtualConsole: console });
  const w = dom.window;
  w.HTMLElement.prototype.scrollIntoView = function () {};
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; };
  w.Range.prototype.getClientRects = () => [];
  w.Range.prototype.getBoundingClientRect = () => ({ left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 });
  const requests = [];
  const note = { id: "note", path: "Start.md", name: "Start.md", text: "# Source\n\nA human [[Other note]] link.\n", revision: "original", editable: !options.protected,
    reason: options.protected ? "Generated note is read-only." : "", recovery_key: "source", newline: "lf", bom: false, override_available: Boolean(options.protected) };
  const workspace = { id: "workspace", name: "Notes", path: "/notes", editable: true, truncated: false,
    entries: [{ path: note.path, name: note.name, kind: "file", editable: note.editable, reason: note.reason }] };
  const outcome = { preview_identity: "reviewed-identity", note_count: 3, edge_count: 2, override_count: 1, destination: "/notes linked Markdown" };
  w.fetch = async (url, request = {}) => {
    const body = JSON.parse(request.body || "{}");
    requests.push({ url, body });
    let result;
    if (url.endsWith("/open") || url.endsWith("/tree")) result = { workspace };
    else if (url.endsWith("/index")) result = { documents: [note], relations: [], warnings: [], truncated: false };
    else if (url.endsWith("/graph-preferences")) result = { preferences: {} };
    else if (url.endsWith("/read")) result = { document: note };
    else if (url.endsWith("/export-links-preview")) result = options.preview ? await options.preview(body) : { status: "preview", ...outcome };
    else if (url.endsWith("/export-links")) result = options.apply ? await options.apply(body) : { status: "exported", ...outcome, destination: body.destination };
    else throw new Error(`Unexpected request: ${url}`);
    return { ok: !result.error, status: result.error ? 400 : 200, json: async () => structuredClone(result) };
  };
  for (const name of ["workspace-vendor.js", "app.js", "workspace.js"]) {
    const script = w.document.createElement("script"); script.textContent = asset(name); w.document.body.append(script);
  }
  t.after(() => { w.close(); assert.deepEqual(errors, []); });
  const el = (id) => w.document.getElementById(id);
  const click = (id) => el(id).click();
  const open = async () => {
    click("workspace-welcome-open"); el("workspace-folder-path").value = "/notes";
    el("workspace-folder-form").dispatchEvent(new w.Event("submit", { cancelable: true }));
    await until(() => w.document.querySelector(".tree-row"), "workspace files");
    w.document.querySelector(".tree-row").click();
    await until(() => w.document.querySelector(".cm-editor"), "open note");
    await until(() => el("workspace-index-status").hidden, "completed workspace index");
  };
  const show = () => { click("workspace-file-actions"); click("workspace-file-export"); };
  const preview = async () => {
    el("workspace-export-form").dispatchEvent(new w.Event("submit", { cancelable: true }));
    await until(() => !el("workspace-export-preview").disabled, "export preview completed");
  };
  const change = (id, value, event = "input") => { el(id).value = value; el(id).dispatchEvent(new w.Event(event, { bubbles: true })); };
  const append = (text) => {
    const view = w.WorkspaceKit.EditorView.findFromDOM(w.document.querySelector(".cm-editor"));
    view.dispatch({ changes: { from: view.state.doc.length, insert: text }, userEvent: "input" });
  };
  return { w, el, click, open, show, preview, change, append, requests };
}

test("linked Markdown export remains inert until preview and applies the exact reviewed identity", async (t) => {
  const app = harness(t, { protected: true }); await app.open();
  app.click("workspace-file-actions");
  assert.equal(app.el("workspace-file-rename").disabled, true);
  assert.equal(app.el("workspace-file-export").disabled, false);
  app.click("workspace-file-export");
  assert.equal(app.el("workspace-export-dialog").open, true);
  assert.equal(app.el("workspace-export-cap").value, "8");
  assert.equal(app.el("workspace-export-apply").disabled, true);
  assert.equal(app.requests.some(({ url }) => url.includes("export-links")), false);
  await app.preview();
  assert.match(app.el("workspace-export-summary").textContent, /3 notes.*2 automatic connections.*notes with personal edits: 1/);
  assert.equal(app.requests.filter(({ url }) => url.endsWith("/export-links")).length, 0);
  app.click("workspace-export-apply");
  await until(() => app.el("workspace-export-summary").textContent.includes("Exported to"), "export success");
  const calls = app.requests.filter(({ url }) => url.includes("export-links"));
  assert.deepEqual(calls.map(({ body }) => body), [
    { workspace_id: "workspace", destination: "/notes linked Markdown", degree_cap: 8 },
    { workspace_id: "workspace", destination: "/notes linked Markdown", degree_cap: 8, preview_identity: "reviewed-identity" },
  ]);
  assert.equal(app.requests.filter(({ url }) => url.endsWith("/open")).length, 1, "Export does not switch the workspace");
  assert.equal(app.el("workspace-export-apply").disabled, true);
  assert.equal(app.w.workspaceHasUnsavedChanges(), false);
  assert.ok(app.requests.every(({ url }) => url.startsWith("/api/workspace/")));
});

test("changing export destination or density requires another preview", async (t) => {
  const app = harness(t); await app.open(); app.show(); await app.preview();
  app.change("workspace-export-destination", "/another export");
  assert.equal(app.el("workspace-export-apply").disabled, true);
  app.click("workspace-export-apply");
  assert.equal(app.requests.some(({ url }) => url.endsWith("/export-links")), false);
  await app.preview();
  app.change("workspace-export-cap", "4", "change");
  assert.equal(app.el("workspace-export-apply").disabled, true);
  await app.preview(); app.click("workspace-export-apply");
  await until(() => app.el("workspace-export-summary").textContent.includes("Exported to"), "changed export completed");
  assert.equal(app.requests.find(({ url }) => url.endsWith("/export-links")).body.degree_cap, 4);
});

test("unsaved or recovery content cannot be silently omitted from an export", async (t) => {
  const app = harness(t); await app.open(); app.append("An unsaved personal addition.\n"); app.show();
  assert.equal(app.el("workspace-export-dialog").open, false);
  assert.match(app.el("workspace-message").textContent, /Save or review all drafts before exporting/);
  assert.equal(app.requests.some(({ url }) => url.includes("export-links")), false);
});

test("new edits after preview block export instead of exporting the older saved note", async (t) => {
  const app = harness(t); await app.open(); app.show(); await app.preview();
  app.append("A later unsaved change.\n"); app.click("workspace-export-apply");
  assert.match(app.el("workspace-export-error").textContent, /Save or review all drafts/);
  assert.equal(app.el("workspace-export-apply").disabled, true);
  assert.equal(app.requests.some(({ url }) => url.endsWith("/export-links")), false);
});

test("failed or stale export responses require a fresh preview and never retry implicitly", async (t) => {
  const app = harness(t, { apply: async () => ({ error: "Saved evidence changed; preview again." }) });
  await app.open(); app.show(); await app.preview(); app.click("workspace-export-apply");
  await until(() => app.el("workspace-export-error").textContent, "export failure");
  assert.match(app.el("workspace-export-error").textContent, /Saved evidence changed/);
  assert.equal(app.el("workspace-export-apply").disabled, true);
  app.click("workspace-export-apply");
  assert.equal(app.requests.filter(({ url }) => url.endsWith("/export-links")).length, 1);
});

test("an incomplete preview cannot enable an unreviewable export", async (t) => {
  const app = harness(t, { preview: async () => ({ status: "preview", preview_identity: "missing-counts" }) });
  await app.open(); app.show(); await app.preview();
  assert.match(app.el("workspace-export-error").textContent, /could not be verified/);
  assert.equal(app.el("workspace-export-apply").disabled, true);
  assert.equal(app.requests.some(({ url }) => url.endsWith("/export-links")), false);
});

test("pending export protects close and controls until its outcome arrives", async (t) => {
  let finish;
  const pending = new Promise((resolve) => { finish = resolve; });
  const app = harness(t, { apply: () => pending }); await app.open(); app.show(); await app.preview();
  app.click("workspace-export-apply");
  assert.equal(app.w.workspaceHasUnsavedChanges(), true);
  assert.equal(app.el("workspace-export-close").disabled, true);
  assert.equal(app.el("workspace-export-destination").disabled, true);
  const cancel = new app.w.Event("cancel", { cancelable: true });
  app.el("workspace-export-dialog").dispatchEvent(cancel);
  assert.equal(cancel.defaultPrevented, true);
  assert.equal(app.el("workspace-export-dialog").open, true);
  finish({ status: "exported", preview_identity: "reviewed-identity", note_count: 3, edge_count: 2, override_count: 1, destination: "/notes linked Markdown" });
  await until(() => app.el("workspace-export-summary").textContent.includes("Exported to"), "pending export settled");
  assert.equal(app.w.workspaceHasUnsavedChanges(), false);
  app.click("workspace-export-close"); assert.equal(app.el("workspace-export-dialog").open, false);
});
