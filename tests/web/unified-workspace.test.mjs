import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { JSDOM, VirtualConsole } from "jsdom";

const asset = (name) => readFileSync(new URL(`../../src/auto_zettelkasten/web/${name}`, import.meta.url), "utf8");
const sourceURI = "zotero://open-pdf/library/items/PDF00001?page=2&annotation=ANNO0001";
const itemURI = "zotero://select/library/items/ITEM0001";
const baseText = `---\ntitle: Example source\nzotero_item_uri: "${itemURI}"\nsource_pdf_uri: "${sourceURI}"\n---\n\n# Example source\n\nSubstantive evidence. [Original PDF](${sourceURI})\n`;
async function until(check, label = "condition") {
  for (let at = 0; at < 400; at++) { if (check()) return; await new Promise((resolve) => setTimeout(resolve, 5)); }
  assert.fail(`Timed out waiting for ${label}`);
}
function delayed() { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; }

function harness(t, options = {}) {
  const errors = []; const virtualConsole = new VirtualConsole(); virtualConsole.on("jsdomError", (error) => errors.push(error));
  const dom = new JSDOM(asset("index.html"), { url: "http://127.0.0.1:8765", runScripts: "dangerously", pretendToBeVisual: true, virtualConsole });
  const w = dom.window, d = w.document;
  w.HTMLElement.prototype.scrollIntoView = function () {};
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; };
  w.Range.prototype.getClientRects = () => [];
  w.Range.prototype.getBoundingClientRect = () => ({ left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 });
  w.HTMLElement.prototype.setPointerCapture = function () {};
  w.HTMLElement.prototype.releasePointerCapture = function () {};
  const requests = [], packets = [], external = [], saved = [];
  const data = { disk: { id: "note-1", path: "Source.md", name: "Source.md", text: baseText, revision: "r1", editable: true, reason: "", bom: false, newline: "lf", recovery_key: "source-note", ...options.document }, failSave: false };
  const workspace = { id: "workspace-one", name: "Synthetic source workspace", path: "/private/synthetic-workspace", editable: true, entries: [{ path: "Source.md", name: "Source.md", kind: "file", editable: true }], truncated: false };
  const reader = { id: "users/0:PDF00001", title: "Example article", scope: "users/0", attachment_key: "PDF00001", item_key: "ITEM0001", url: "/api/zotero/attachments/synthetic-capability/file", pdf_identity: "synthetic-pdf-revision", note_path: "Source.md", location: { pageIndex: 1, annotationID: "ANNO0001" }, readOnly: true, annotations: [{ id: "ANNO0001", version: 1, type: "highlight", text: "Substantive evidence", position: { pageIndex: 1, rects: [[1, 2, 3, 4]] } }], ...options.reader };
  const page = (records) => ({ records, start: 0, limit: 64, total: records.length, next_start: null, library_version: 1, server_id: "SYNTHETIC001" });
  w.open = (...args) => { external.push(args); return null; };
  w.fetch = async (url, request = {}) => {
    const body = request.body ? JSON.parse(request.body) : {}; requests.push({ url, body });
    let status = 200, result;
    const custom = await options.fetch?.(url, body, { data, reader });
    if (custom !== undefined) { status = custom.status || 200; result = custom.result ?? custom; }
    else if (["/api/workspace/open", "/api/workspace/tree"].includes(url)) result = { workspace };
    else if (url === "/api/workspace/index") result = { documents: [{ ...data.disk }], relations: [], warnings: [], truncated: false };
    else if (url === "/api/workspace/graph-preferences") result = { preferences: { version: 1, colorBy: "none", origins: { managed: true, personal: true, unknown: true } } };
    else if (url === "/api/workspace/read") result = { document: { ...data.disk } };
    else if (["/api/workspace/save", "/api/workspace/save-override"].includes(url)) {
      if (data.failSave) { status = 500; result = { error: "Synthetic disk failure" }; }
      else { saved.push(body.text); data.disk = { ...data.disk, text: body.text, revision: `r${saved.length + 1}` }; result = { document: data.disk }; }
    }
    else if (["/api/zotero/open-source", "/api/zotero/open-pdf"].includes(url)) result = { reader: structuredClone(reader) };
    else if (url === "/api/zotero/annotation-refresh") result = { annotations: structuredClone(reader.annotations) };
    else if (url === "/api/zotero/status") result = { connected: true, writable: false, writes_available: false, server_id: "SYNTHETIC001" };
    else if (url === "/api/zotero/libraries") result = { libraries: [{ scope: "users/0", name: "My Library", writable: false }] };
    else if (url === "/api/zotero/collections") result = page([]);
    else if (url === "/api/zotero/items") result = page([{ key: "ITEM0001", version: 1, note_path: "Source.md", data: { key: "ITEM0001", version: 1, itemType: "journalArticle", title: "Example article" } }]);
    else if (url === "/api/zotero/children") result = page([{ key: "PDF00001", data: { key: "PDF00001", parentItem: "ITEM0001", itemType: "attachment", contentType: "application/pdf", title: "Example PDF" } }]);
    else throw new Error(`Unexpected synthetic API request ${url}`);
    return { ok: status < 400, status, json: async () => structuredClone(result) };
  };
  for (const name of ["workspace-vendor.js", "app.js", "workspace-library.js", "workspace-source-reader.js", "workspace.js"]) {
    const script = d.createElement("script"); script.textContent = asset(name); d.body.append(script);
  }
  const el = (id) => d.getElementById(id), click = (id) => el(id).click();
  const getView = () => w.WorkspaceKit.EditorView.findFromDOM(d.querySelector(".cm-editor"));
  const open = async () => {
    click("workspace-welcome-open"); el("workspace-folder-path").value = workspace.path;
    el("workspace-folder-form").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
    await until(() => d.querySelector(".tree-row"), "workspace tree"); d.querySelector(".tree-row").click();
    await until(() => d.querySelector(".cm-editor"), "note buffer");
  };
  const mode = (value) => d.querySelector(`[data-mode="${value}"]`).click();
  const append = (value) => { const view = getView(); view.dispatch({ changes: { from: view.state.doc.length, insert: value }, userEvent: "input" }); };
  const emit = (frame, type, fields = {}, overrides = {}) => {
    const instanceId = new URL(frame.src).hash.slice("#instance=".length);
    w.dispatchEvent(new w.MessageEvent("message", { origin: w.location.origin, source: frame.contentWindow, data: { channel: "az-zotero-reader-v1", instanceId, type, ...fields }, ...overrides }));
  };
  const connectFrame = (frame, { acknowledgeClose = true } = {}) => {
    frame.contentWindow.postMessage = (packet, origin) => {
      packets.push({ packet: JSON.parse(JSON.stringify(packet)), origin });
      if (packet.type === "close" && acknowledgeClose) queueMicrotask(() => emit(frame, "closed"));
    };
    emit(frame, "ready"); emit(frame, "initialized"); return frame;
  };
  const openPDF = async ({ ready = true } = {}) => {
    click("workspace-source-open"); await until(() => d.querySelector(".source-reader-frame"), "paired PDF iframe");
    const frame = d.querySelector(".source-reader-frame"); return ready ? connectFrame(frame) : frame;
  };
  t.after(() => { w.close(); assert.deepEqual(errors, [], "No uncaught application exceptions"); });
  return { w, d, el, click, data, reader, requests, packets, external, saved, getView, open, mode, append, emit, connectFrame, openPDF };
}

test("Source PDF is available in each note mode and routes exact deep links", async (t) => {
  const app = harness(t); await app.open();
  for (const mode of ["source", "live", "reading"]) {
    app.mode(mode); assert.equal(app.el("workspace-source-open").hidden, false);
    app.click("workspace-source-open"); await until(() => app.requests.some((row) => row.url === "/api/zotero/open-source"));
    assert.equal(app.requests.filter((row) => row.url === "/api/zotero/open-source").at(-1).body.uri, sourceURI);
    assert.equal(app.requests.filter((row) => row.url === "/api/zotero/open-source").at(-1).body.workspace_id, "workspace-one");
    await until(() => app.d.querySelector('[data-tab-key="source:users/0:PDF00001"]'));
  }
  assert.equal(app.d.querySelectorAll(".source-reader-frame").length, 1);
  assert.equal(app.d.querySelectorAll('[data-tab-key="note:Source.md"]').length, 1);
  assert.equal(app.d.querySelectorAll('[data-tab-key="source:users/0:PDF00001"]').length, 1);
});

test("property and Reading links route to the source reader instead of missing-note creation", async (t) => {
  const app = harness(t); await app.open(); app.mode("reading");
  await until(() => app.el("workspace-reader").querySelector("[data-source-uri]"), "rendered Zotero link");
  app.el("workspace-reader").querySelector("[data-source-uri]").click(); await until(() => app.d.querySelector(".source-reader-frame"));
  assert.equal(app.requests.some((row) => row.url === "/api/workspace/create"), false);
  assert.equal(app.requests.filter((row) => row.url === "/api/zotero/open-source").at(-1).body.uri, sourceURI);
  assert.equal(Object.hasOwn(app.requests.filter((row) => row.url === "/api/zotero/open-source").at(-1).body, "note_path"), false, "general source links do not assert that the currently open note owns the target source");
  const property = [...app.el("workspace-properties").querySelectorAll("[data-source-uri]")].find((row) => row.dataset.sourceUri === itemURI);
  assert.ok(property, "Zotero item URI remains clickable in properties"); property.click();
  await until(() => app.requests.filter((row) => row.url === "/api/zotero/open-source").at(-1).body.uri === itemURI);
});

test("opening the PDF and Zotero child note reuses the dirty editor buffer", async (t) => {
  const app = harness(t); await app.open(); const view = app.getView(); app.append("\nPersonal unsaved thought.");
  await app.openPDF(); assert.equal(app.getView(), view); assert.match(view.state.doc.toString(), /Personal unsaved thought/);
  assert.equal(app.el("workspace-pdf-divider").hidden, false);
  app.click("workspace-zotero-view"); await until(() => app.d.querySelector('[data-item-key="ITEM0001"]'), "Zotero tree");
  app.d.querySelector('[data-item-key="ITEM0001"]').click(); await until(() => app.d.querySelector(".zotero-open-note"));
  app.d.querySelector(".zotero-open-note").click(); await until(() => app.el("workspace-pdf-pane").hidden);
  assert.equal(app.getView(), view); assert.match(view.state.doc.toString(), /Personal unsaved thought/);
  assert.equal(app.d.querySelectorAll('[data-tab-key="note:Source.md"]').length, 1);
  assert.equal(app.requests.filter((row) => row.url === "/api/workspace/save").length, 0);
});

test("closing a paired PDF preserves its unsaved note and recovery draft", async (t) => {
  const app = harness(t); await app.open(); app.append("\nUnsent draft survives PDF closure."); await app.openPDF();
  const source = app.d.querySelector('[data-tab-key="source:users/0:PDF00001"]'); source.querySelector(".tab-close").click();
  await until(() => !app.d.querySelector('[data-tab-key="source:users/0:PDF00001"]'), "PDF close acknowledgement");
  assert.match(app.getView().state.doc.toString(), /Unsent draft survives/);
  assert.ok(app.d.querySelector('[data-tab-key="note:Source.md"]'));
  assert.equal(app.w.workspaceHasUnsavedChanges(), true);
  assert.match(app.w.localStorage.getItem("az.workspace.draft.source-note"), /Unsent draft survives/);
});

test("split divider is keyboard-operable and expansion preserves note mode", async (t) => {
  const app = harness(t); await app.open(); await app.openPDF(); app.mode("reading");
  const divider = app.el("workspace-pdf-divider");
  const pointer = new app.w.MouseEvent("pointerdown", { button: 0, bubbles: true, cancelable: true });
  Object.defineProperty(pointer, "pointerId", { value: 1 }); divider.dispatchEvent(pointer);
  assert.equal(app.d.activeElement, divider, "clicking the divider gives subsequent arrow keys the separator focus");
  divider.dispatchEvent(new app.w.MouseEvent("pointerup", { button: 0, bubbles: true }));
  app.d.activeElement.dispatchEvent(new app.w.KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true }));
  assert.ok(Number(divider.getAttribute("aria-valuenow")) > 50);
  divider.dispatchEvent(new app.w.KeyboardEvent("keydown", { key: "End", bubbles: true, cancelable: true })); assert.equal(divider.getAttribute("aria-valuenow"), "80");
  divider.dispatchEvent(new app.w.KeyboardEvent("keydown", { key: "Home", bubbles: true, cancelable: true })); assert.equal(divider.getAttribute("aria-valuenow"), "20");
  app.click("workspace-pdf-expand"); assert.ok(app.el("workspace-document-area").classList.contains("source-pdf-only"));
  app.click("workspace-pdf-expand"); app.click("workspace-note-expand"); assert.ok(app.el("workspace-document-area").classList.contains("source-note-only"));
  assert.equal(app.el("workspace-reader").hidden, false);
  assert.equal(app.d.querySelector('[data-mode="reading"]').getAttribute("aria-pressed"), "true");
});

test("graph and source tabs switch without changing note text or duplicating source tabs", async (t) => {
  const app = harness(t); await app.open(); app.append("\nA draft across views."); await app.openPDF({ ready: false });
  const before = app.getView().state.doc.toString(); app.click("workspace-graph-open");
  await until(() => !app.el("workspace-global-graph").hidden, "global graph"); assert.equal(app.el("workspace-pdf-pane").hidden, true);
  app.d.querySelector('[data-tab-key="source:users/0:PDF00001"] [role="tab"]').click();
  await until(() => !app.el("workspace-pdf-pane").hidden, "paired tab restored");
  assert.equal(app.el("workspace-global-graph").hidden, true); assert.equal(app.getView().state.doc.toString(), before);
  assert.equal(app.d.querySelectorAll('[data-tab-key="source:users/0:PDF00001"]').length, 1);
});

test("failed note saves leave the paired draft available for a later successful save", async (t) => {
  const app = harness(t); await app.open(); await app.openPDF(); app.append("\nDraft despite write error."); app.data.failSave = true;
  app.click("workspace-save"); await until(() => app.el("workspace-message").textContent.includes("Synthetic disk failure"));
  assert.match(app.getView().state.doc.toString(), /Draft despite write error/); assert.equal(app.w.workspaceHasUnsavedChanges(), true);
  assert.equal(app.el("workspace-pdf-pane").hidden, false); assert.equal(app.saved.length, 0);
  app.data.failSave = false; app.click("workspace-save"); await until(() => app.saved.length === 1);
  assert.match(app.saved[0], /Draft despite write error/); await until(() => !app.w.workspaceHasUnsavedChanges());
});

test("reader message bridge rejects other frames and origins, keeping PDF identity bound", async (t) => {
  const app = harness(t); await app.open(); const frame = await app.openPDF({ ready: false });
  frame.contentWindow.postMessage = (packet, origin) => app.packets.push({ packet, origin });
  app.emit(frame, "ready", {}, { origin: "https://example.invalid" }); app.emit(frame, "ready", {}, { source: app.w });
  assert.equal(app.packets.length, 0); app.emit(frame, "ready");
  assert.equal(app.packets[0].packet.type, "init"); assert.equal(app.packets[0].packet.document.url, app.reader.url);
  assert.equal(app.packets[0].packet.readOnly, true); assert.equal(app.packets[0].origin, app.w.location.origin);
});

test("migrated properties preserve protected source identity while personal metadata stays editable", async (t) => {
  const app = harness(t, { document: { live_note: true, live_binding: { library_type: "user", library_id: "0", item_key: "ITEM0001", server_id: "SYNTHETIC001" }, live_protected_fields: ["zotero_item_uri", "source_pdf_uri"], property_ownership: { title: "personal", zotero_item_uri: "generated", source_pdf_uri: "generated" }, override_available: false } });
  await app.open(); app.mode("live");
  assert.equal(app.d.querySelector('[aria-label="Edit property zotero_item_uri"]'), null);
  assert.equal(app.d.querySelector('[aria-label="Edit property source_pdf_uri"]'), null);
  assert.ok(app.d.querySelector('[aria-label="Edit property title"]'));
  assert.ok(app.el("workspace-properties").querySelector('[data-source-uri]'));
});

test("migrated inline title updates active Markdown metadata without renaming identity", async (t) => {
  const app = harness(t, { document: { live_note: true, live_binding: { library_type: "user", library_id: "0", item_key: "ITEM0001", server_id: "SYNTHETIC001" }, live_protected_fields: ["zotero_item_uri", "source_pdf_uri"], override_available: false }, fetch: (url, body, { data }) => {
    if (url === "/api/workspace/save") {
      data.disk = { ...data.disk, text: body.text, display_title: "A clearer personal title", personal_display_title: "A clearer personal title", revision: "metadata-new" };
      return { document: data.disk };
    }
  } });
  await app.open(); app.click("workspace-file-title"); app.el("workspace-title-input").value = "A clearer personal title";
  app.el("workspace-title-form").dispatchEvent(new app.w.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => app.el("workspace-file-title").textContent === "A clearer personal title", "shared title metadata saved");
  assert.equal(app.data.disk.path, "Source.md"); assert.equal(app.data.disk.id, "note-1");
  assert.equal(app.requests.some((row) => row.url.includes("relocate")), false);
  assert.equal(app.w.WorkspaceKit.inspectProperties(app.getView().state.doc.toString()).rows.find((row) => row.key === "title").value, "A clearer personal title");
  assert.match(app.getView().state.doc.toString(), /# Example source/);
  assert.ok(app.getView().state.doc.toString().includes(itemURI));
});

test("failed annotation save is retained and explicit reconciliation unblocks closure", async (t) => {
  const app = harness(t, { reader: { readOnly: false }, fetch: (url, body, { reader }) => {
    if (url === "/api/zotero/annotation-save") return { pending: true, warning: "Synthetic uncertain write retained locally" };
    if (url === "/api/zotero/annotation-reconcile") return { resolved: true, annotations: reader.annotations };
  } });
  await app.open(); const frame = await app.openPDF();
  assert.equal(app.el("workspace-pdf-retry").hidden, true);
  app.emit(frame, "save-annotations", { operationId: "synthetic-operation", annotations: [{ ...app.reader.annotations[0], comment: "Pending comment" }] });
  await until(() => app.el("workspace-message").textContent.includes("uncertain write"));
  assert.equal(app.el("workspace-pdf-retry").hidden, false);
  assert.equal(app.w.workspaceHasUnsavedChanges(), true);
  app.d.querySelector('[data-tab-key="source:users/0:PDF00001"] .tab-close').click();
  await until(() => app.el("workspace-message").textContent.includes("pending annotation"));
  assert.ok(app.d.querySelector('[data-tab-key="source:users/0:PDF00001"]'));
  app.click("workspace-pdf-refresh"); await until(() => app.requests.some((row) => row.url === "/api/zotero/annotation-reconcile"));
  await until(() => !app.w.workspaceHasUnsavedChanges());
  assert.equal(app.el("workspace-pdf-retry").hidden, true);
  assert.deepEqual(app.requests.find((row) => row.url === "/api/zotero/annotation-reconcile").body.request_ids, ["synthetic-operation"]);
  assert.equal(app.requests.filter((row) => row.url === "/api/zotero/annotation-save").length, 1, "reconciliation does not blindly repeat the save");
  app.d.querySelector('[data-tab-key="source:users/0:PDF00001"] .tab-close').click();
  await until(() => !app.d.querySelector('[data-tab-key="source:users/0:PDF00001"]'));
});

test("inactive close acknowledgements do not strand a quickly reopened PDF", async (t) => {
  const app = harness(t); await app.open(); const frame = await app.openPDF({ ready: false }); app.connectFrame(frame, { acknowledgeClose: false });
  app.emit(frame, "view-state", { viewState: { pageIndex: 4, scale: "page-width" }, primary: true });
  app.click("workspace-graph-open"); await until(() => app.packets.some((entry) => entry.packet.type === "close"));
  app.click("workspace-graph-open");
  assert.equal(app.packets.filter((entry) => entry.packet.type === "close").length, 1, "one close handshake per inactive frame");
  app.d.querySelector('[data-tab-key="source:users/0:PDF00001"] [role="tab"]').click(); await until(() => !app.el("workspace-pdf-pane").hidden);
  app.emit(frame, "closed"); await until(() => app.d.querySelector(".source-reader-frame") && app.d.querySelector(".source-reader-frame") !== frame, "active reader remounted after late close");
  const replacement = app.d.querySelector(".source-reader-frame"); assert.ok(replacement); app.connectFrame(replacement);
  assert.deepEqual(app.packets.filter((row) => row.packet.type === "init").at(-1).packet.viewState, { pageIndex: 4, scale: "page-width" });
  assert.equal(app.el("workspace-pdf-pane").hidden, false);
});

test("saving a dirty shared note projects confirmed changes from each PDF once", async (t) => {
  const secondURI = "zotero://open-pdf/library/items/PDF00002?page=1";
  const projections = [];
  const app = harness(t, { document: { text: `${baseText}\n[Second PDF](${secondURI})\n`, live_note: true, live_binding: { library_type: "user", library_id: "0", item_key: "ITEM0001", server_id: "SYNTHETIC001" } }, reader: { readOnly: false }, fetch: (url, body, { data, reader }) => {
    if (url === "/api/zotero/open-source" && body.uri === secondURI) return { reader: { ...reader, id: "users/0:PDF00002", attachment_key: "PDF00002", title: "Second attachment", url: "/api/zotero/attachments/second-synthetic-capability/file" } };
    if (url === "/api/zotero/annotation-save") return { annotations: reader.annotations };
    if (url === "/api/zotero/annotation-project") {
      projections.push(body);
      data.disk = { ...data.disk, text: `${data.disk.text}\nConfirmed ${body.attachment_key} annotation.\n`, revision: `projection-${projections.length}` };
      return { document: data.disk };
    }
  } });
  await app.open(); app.append("\nMy pending prose."); const first = await app.openPDF();
  app.emit(first, "save-annotations", { operationId: "first-confirmed-edit", annotations: [{ ...app.reader.annotations[0], comment: "First attachment comment" }] });
  await until(() => app.packets.some((row) => row.packet.type === "operation-result" && row.packet.operationId === "first-confirmed-edit"));
  app.mode("reading");
  const secondLink = [...app.el("workspace-reader").querySelectorAll("[data-source-uri]")].find((row) => row.dataset.sourceUri === secondURI);
  assert.ok(secondLink); secondLink.click();
  await until(() => app.d.querySelector('[data-tab-key="source:users/0:PDF00002"]'));
  const second = [...app.d.querySelectorAll(".source-reader-frame")].find((frame) => frame !== first); assert.ok(second); app.connectFrame(second);
  app.emit(second, "save-annotations", { operationId: "second-confirmed-edit", annotations: [{ ...app.reader.annotations[0], comment: "Second attachment comment" }] });
  await until(() => app.packets.some((row) => row.packet.type === "operation-result" && row.packet.operationId === "second-confirmed-edit"));
  assert.equal(projections.length, 0, "confirmed Zotero saves do not overwrite an unsaved Markdown draft");
  app.click("workspace-save"); await until(() => projections.length === 2, "both deferred attachment projections");
  assert.deepEqual(projections.map((row) => row.attachment_key).sort(), ["PDF00001", "PDF00002"]);
  assert.equal(projections[1].revision, "projection-1", "later projection follows the updated document revision");
  assert.match(app.getView().state.doc.toString(), /My pending prose/);
  assert.match(app.getView().state.doc.toString(), /Confirmed PDF00001 annotation/);
  assert.match(app.getView().state.doc.toString(), /Confirmed PDF00002 annotation/);
});

test("discarding a conflicted draft resumes its confirmed annotation projection", async (t) => {
  const projections = [];
  const app = harness(t, { document: { live_note: true, live_binding: { library_type: "user", library_id: "0", item_key: "ITEM0001", server_id: "SYNTHETIC001" } }, reader: { readOnly: false }, fetch: (url, body, { data, reader }) => {
    if (url === "/api/zotero/annotation-save") return { annotations: reader.annotations };
    if (url === "/api/workspace/save") {
      data.disk = { ...data.disk, text: `${baseText}\nExternal editor update.\n`, revision: "external-revision" };
      return { status: 409, result: { error: "Changed externally", code: "conflict", current: data.disk } };
    }
    if (url === "/api/zotero/annotation-project") {
      projections.push(body); data.disk = { ...data.disk, text: `${data.disk.text}\nConfirmed annotation after discard.\n`, revision: "projected-after-discard" };
      return { document: data.disk };
    }
  } });
  await app.open(); app.append("\nDraft to discard."); const frame = await app.openPDF();
  app.emit(frame, "save-annotations", { operationId: "saved-before-discard", annotations: [{ ...app.reader.annotations[0], comment: "Confirmed comment" }] });
  await until(() => app.packets.some((row) => row.packet.type === "operation-result"));
  app.click("workspace-save"); await until(() => !app.el("workspace-conflict").hidden, "external edit conflict");
  assert.equal(projections.length, 0);
  app.click("workspace-use-disk"); await until(() => app.el("workspace-confirm-dialog").open); app.click("workspace-confirm-accept");
  await until(() => projections.length === 1, "projection after explicit discard");
  assert.equal(projections[0].revision, "external-revision");
  assert.match(app.getView().state.doc.toString(), /External editor update/);
  assert.match(app.getView().state.doc.toString(), /Confirmed annotation after discard/);
  assert.doesNotMatch(app.getView().state.doc.toString(), /Draft to discard/);
});

test("successful annotation responses with an empty pending queue do not block the reader", async (t) => {
  const app = harness(t, { reader: { readOnly: false }, fetch: (url, body, { reader }) => {
    if (url === "/api/zotero/annotation-save") return { pending: [], annotations: reader.annotations, resolved: true };
  } });
  await app.open(); const frame = await app.openPDF();
  app.emit(frame, "save-annotations", { operationId: "confirmed-empty-pending", annotations: [{ ...app.reader.annotations[0], comment: "Confirmed comment" }] });
  await until(() => app.packets.some((row) => row.packet.type === "operation-result" && row.packet.operationId === "confirmed-empty-pending"));
  const outcome = app.packets.find((row) => row.packet.type === "operation-result" && row.packet.operationId === "confirmed-empty-pending").packet;
  assert.equal(outcome.success, true); assert.equal(app.w.workspaceHasUnsavedChanges(), false);
  app.d.querySelector('[data-tab-key="source:users/0:PDF00001"] .tab-close').click();
  await until(() => !app.d.querySelector('[data-tab-key="source:users/0:PDF00001"]'));
  assert.equal(app.requests.some((row) => row.url === "/api/zotero/annotation-reconcile"), false);
});

test("annotation save deltas and empty replays preserve unrelated annotations, while deletes are explicit", async (t) => {
  const initial = [
    { id: "ANNO0001", version: 1, type: "highlight", text: "First quotation", comment: "Original comment", position: { pageIndex: 1, rects: [[1, 2, 3, 4]] } },
    { id: "ANNO0002", version: 4, type: "highlight", text: "Another quotation", comment: "Unrelated comment", position: { pageIndex: 2, rects: [[5, 6, 7, 8]] } },
  ];
  let saves = 0;
  const app = harness(t, { reader: { readOnly: false, annotations: initial }, fetch: (url) => {
    if (url === "/api/zotero/annotation-save") {
      saves += 1;
      return { pending: [], annotations: saves === 1 ? [{ ...initial[0], version: 2, comment: "Confirmed edit" }] : [], replayed: saves > 1 };
    }
    if (url === "/api/zotero/annotation-delete") return { pending: [], annotations: [], deleted: ["ANNO0001"] };
    if (url === "/api/zotero/annotation-refresh") return { annotations: [], pending: [] };
  } });
  await app.open(); const frame = await app.openPDF();
  const lastProjection = () => app.packets.filter((row) => row.packet.type === "annotations").at(-1)?.packet;
  const completed = (id) => app.packets.some((row) => row.packet.type === "operation-result" && row.packet.operationId === id);
  app.emit(frame, "save-annotations", { operationId: "delta-edit", annotations: [{ ...initial[0], comment: "Confirmed edit" }] });
  await until(() => completed("delta-edit"));
  assert.deepEqual(lastProjection().annotations.map((row) => row.id), ["ANNO0001", "ANNO0002"]);
  assert.equal(lastProjection().annotations[0].version, 2); assert.equal(lastProjection().annotations[0].comment, "Confirmed edit");
  assert.deepEqual(lastProjection().annotations[1], initial[1]); assert.deepEqual(lastProjection().deletedIds, []);
  app.emit(frame, "delete-annotations", { operationId: "confirmed-delete", ids: ["ANNO0001"] });
  await until(() => completed("confirmed-delete"));
  assert.deepEqual(lastProjection().annotations, [initial[1]]); assert.deepEqual(lastProjection().deletedIds, ["ANNO0001"]);
  assert.equal(app.requests.find((row) => row.url === "/api/zotero/annotation-delete").body.annotations[0].version, 2, "delete uses the confirmed delta's current version");
  app.emit(frame, "save-annotations", { operationId: "delta-edit", annotations: [{ ...initial[0], comment: "Confirmed edit" }] });
  await until(() => saves === 2 && app.packets.filter((row) => row.packet.type === "operation-result" && row.packet.operationId === "delta-edit").length === 2);
  assert.deepEqual(lastProjection().annotations, [initial[1]], "an empty replay neither clears the remaining annotation nor restores a removed one");
  assert.deepEqual(lastProjection().deletedIds, []); assert.equal(app.w.workspaceHasUnsavedChanges(), false);
  app.click("workspace-pdf-refresh"); await until(() => lastProjection().deletedIds.includes("ANNO0002"));
  assert.deepEqual(lastProjection().annotations, [], "an explicit full refresh still replaces the attachment snapshot");
});

test("remount renews only PDF access and preserves the same file's reading state and paired note", async (t) => {
  const app = harness(t, { fetch: (url, body, { reader }) => {
    if (url === "/api/zotero/open-pdf") { const { note_path, ...renewed } = reader; return { reader: { ...renewed, url: "/api/zotero/attachments/renewed-capability/file" } }; }
  } });
  await app.open(); const frame = await app.openPDF();
  assert.equal(app.requests.filter((row) => row.url === "/api/zotero/open-pdf").length, 0, "initial opening already has a fresh capability");
  app.emit(frame, "view-state", { viewState: { pageIndex: 6, scale: 1.25 }, primary: true });
  app.click("workspace-graph-open"); await until(() => !app.d.querySelector(".source-reader-frame"));
  app.d.querySelector('[data-tab-key="source:users/0:PDF00001"] [role="tab"]').click();
  await until(() => app.d.querySelector(".source-reader-frame")); const replacement = app.d.querySelector(".source-reader-frame"); app.connectFrame(replacement);
  const init = app.packets.filter((row) => row.packet.type === "init").at(-1).packet;
  assert.equal(init.document.url, "/api/zotero/attachments/renewed-capability/file");
  assert.deepEqual(init.viewState, { pageIndex: 6, scale: 1.25 }); assert.deepEqual(init.location, {});
  assert.deepEqual(app.requests.find((row) => row.url === "/api/zotero/open-pdf").body, { scope: "users/0", attachment_key: "PDF00001" });
  assert.equal(app.el("workspace-pdf-divider").hidden, false); assert.match(app.getView().state.doc.toString(), /Substantive evidence/);
  assert.equal(app.d.querySelectorAll('[data-tab-key="note:Source.md"]').length, 1);
});

test("a replaced PDF resets old position and highlight instead of replaying stale view state", async (t) => {
  const app = harness(t, { reader: { readOnly: false }, fetch: (url, body, { reader }) => {
    if (url === "/api/zotero/open-pdf") return { reader: { ...reader, pdf_identity: "replacement-file", url: "/api/zotero/attachments/replacement-capability/file", readOnly: true, location: {} } };
  } });
  await app.open(); const frame = await app.openPDF(); app.emit(frame, "view-state", { viewState: { pageIndex: 200, scale: 2 }, primary: true });
  app.w.localStorage.setItem("az.reader.users/0:PDF00001.replacement-file", JSON.stringify({ pageIndex: 99 }));
  app.click("workspace-graph-open"); await until(() => !app.d.querySelector(".source-reader-frame"));
  app.d.querySelector('[data-tab-key="source:users/0:PDF00001"] [role="tab"]').click(); await until(() => app.d.querySelector(".source-reader-frame"));
  app.connectFrame(app.d.querySelector(".source-reader-frame"));
  const init = app.packets.filter((row) => row.packet.type === "init").at(-1).packet;
  assert.equal(init.viewState, null); assert.deepEqual(init.location, {}); assert.equal(init.readOnly, true);
  assert.match(app.el("workspace-message").textContent, /PDF file changed/);
  assert.equal(app.getView().state.doc.toString(), baseText);
});

test("late capability renewals cannot mount after switching away or closing their tab", async (t) => {
  const first = delayed(), second = delayed(); let renewals = 0;
  const app = harness(t, { fetch: async (url, body, { reader }) => {
    if (url === "/api/zotero/open-pdf") { renewals += 1; await (renewals === 1 ? first.promise : second.promise); return { reader: { ...reader, url: "/api/zotero/attachments/late-capability/file" } }; }
  } });
  await app.open(); await app.openPDF(); app.click("workspace-graph-open"); await until(() => !app.d.querySelector(".source-reader-frame"));
  app.d.querySelector('[data-tab-key="source:users/0:PDF00001"] [role="tab"]').click(); await until(() => renewals === 1);
  app.click("workspace-graph-open"); first.resolve(); await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(app.d.querySelector(".source-reader-frame"), null); assert.equal(app.el("workspace-global-graph").hidden, false);
  app.d.querySelector('[data-tab-key="source:users/0:PDF00001"] [role="tab"]').click(); await until(() => renewals === 2);
  app.d.querySelector('[data-tab-key="source:users/0:PDF00001"] .tab-close').click();
  await until(() => !app.d.querySelector('[data-tab-key="source:users/0:PDF00001"]')); second.resolve();
  await new Promise((resolve) => setTimeout(resolve, 20)); assert.equal(app.d.querySelector(".source-reader-frame"), null);
  assert.ok(app.d.querySelector('[data-tab-key="note:Source.md"]'));
});

test("failed or mismatched renewal leaves a retryable source tab without opening a different PDF", async (t) => {
  let attempts = 0;
  const app = harness(t, { fetch: (url, body, { reader }) => {
    if (url === "/api/zotero/open-pdf") {
      attempts += 1;
      if (attempts === 1) throw new Error("Zotero is temporarily unavailable");
      return { reader: { ...reader, id: attempts === 2 ? "wrong-source" : reader.id, url: "/api/zotero/attachments/recovered-capability/file" } };
    }
  } });
  await app.open(); await app.openPDF(); app.click("workspace-graph-open"); await until(() => !app.d.querySelector(".source-reader-frame"));
  const reopen = () => app.d.querySelector('[data-tab-key="source:users/0:PDF00001"] [role="tab"]').click();
  reopen(); await until(() => app.el("workspace-message").textContent.includes("temporarily unavailable"));
  assert.equal(app.d.querySelector(".source-reader-frame"), null); assert.ok(app.d.querySelector('[data-tab-key="source:users/0:PDF00001"]'));
  reopen(); await until(() => app.el("workspace-message").textContent.includes("did not match")); assert.equal(app.d.querySelector(".source-reader-frame"), null);
  reopen(); await until(() => app.d.querySelector(".source-reader-frame")); app.connectFrame(app.d.querySelector(".source-reader-frame"));
  assert.equal(app.packets.filter((row) => row.packet.type === "init").at(-1).packet.document.url, "/api/zotero/attachments/recovered-capability/file");
  assert.equal(attempts, 3, "retries are explicit tab selections");
});
