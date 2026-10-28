import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { JSDOM, VirtualConsole } from "jsdom";

const source = readFileSync(new URL("../../src/auto_zettelkasten/web/workspace-annotations.js", import.meta.url), "utf8");
const receipt = (overrides = {}) => ({ receipt_id: "a".repeat(64), scope: "users/0", attachment_key: "PDFTEST2", status: "checking_outcome", drafts: [{ id: "ANNTEST2", text: "A retained quotation", comment: "Personal interpretation" }], actions: { check: true, retry: false, discard: false }, durable: true, ...overrides });
const job = (overrides = {}) => ({ id: "job-one", note_path: "notes/source.md", document_id: "note-one", revision: "revision-one", scope: "users/0", attachment_key: "PDFTEST2", state: "blocked", reason: "The automatic section was edited.", ...overrides });
async function until(check, label = "condition") { for (let i = 0; i < 150; i++) { if (check()) return; await new Promise((resolve) => setTimeout(resolve, 5)); } assert.fail(`Timed out waiting for ${label}`); }
function harness(t, options = {}) {
  const errors = [], virtualConsole = new VirtualConsole(); virtualConsole.on("jsdomError", (error) => errors.push(error));
  const dom = new JSDOM('<button id="trigger">Annotation changes</button><div id="panel"></div>', { runScripts: "dangerously", url: "http://127.0.0.1:8765", pretendToBeVisual: true, virtualConsole });
  const window = dom.window, container = window.document.getElementById("panel"); window.eval(source);
  const state = { records: [receipt()], jobs: [], next: null, document: { id: "note-one", revision: "revision-one", text: "Source text" }, workspace: { id: "workspace-one" }, ...options.state };
  const calls = [], updates = [], pdfs = [], notes = [], messages = [];
  const panel = window.createAnnotationPanel({ container, getWorkspace: () => state.workspace, getDocument: async () => state.document,
    onUpdated: (value, context) => updates.push({ value: JSON.parse(JSON.stringify(value)), context: JSON.parse(JSON.stringify(context)) }),
    onOpenPDF: (value) => pdfs.push(JSON.parse(JSON.stringify(value))), onOpenNote: (path) => notes.push(path), message: (...args) => messages.push(args),
    api: async (name, body) => {
      calls.push({ name, body: JSON.parse(JSON.stringify(body)) });
      const override = await options.api?.(name, body, state); if (override !== undefined) return override;
      if (name === "zotero/annotation-recovery") return { records: state.records, next_start: state.next, total: state.records.length, server_id: "SYNTHETIC001" };
      if (name === "zotero/annotation-sync-status") return { jobs: state.jobs };
      if (["zotero/annotation-reconcile", "zotero/annotation-retry", "zotero/annotation-discard"].includes(name)) return { resolved: true, annotations: [] };
      if (name === "zotero/annotation-projection-preview") return { preview_token: "review-token", revision: body.revision, state: "conflict", existing_section: "My edited section", proposed_section: "Confirmed Zotero quotation", choices: ["preserve", "replace", "pause"] };
      if (name === "zotero/annotation-projection-resolve") return { document: { ...state.document, revision: "revision-two" } };
      throw new Error(`Unexpected ${name}`);
    },
  });
  const byText = (label) => [...container.querySelectorAll("button")].find((button) => button.textContent === label);
  const click = (label) => { const value = byText(label); assert.ok(value, label); value.click(); };
  const show = async () => { panel.show(); await until(() => calls.some((row) => row.name === "zotero/annotation-sync-status") && container.querySelector(".annotation-change"), "annotation inventory"); };
  t.after(() => { panel.destroy(); window.close(); assert.deepEqual(errors, []); });
  return { panel, state, calls, updates, pdfs, notes, messages, container, window, show, click, byText };
}

test("pending changes are discovered from durable receipts without resending and unsafe markup remains text", async (t) => {
  const app = harness(t, { state: { records: [receipt({ title: '<img src=x onerror="window.compromised=true">', drafts: [{ text: "<script>bad</script>" }] })] } });
  assert.equal(app.calls.length, 0); assert.ok(app.container.hidden); await app.show();
  assert.match(app.container.textContent, /Checking outcome/); assert.equal(app.container.querySelector("img,script"), null);
  assert.equal(app.window.compromised, undefined); assert.equal(app.byText("Retry saved changes"), undefined); assert.equal(app.byText("Keep Zotero version"), undefined);
  assert.equal(app.calls.some((row) => /save|retry|discard/.test(row.name)), false);
  app.click("Check saved state"); await until(() => app.updates.length);
  assert.deepEqual(app.calls.find((row) => row.name === "zotero/annotation-reconcile").body, { scope: "users/0", attachment_key: "PDFTEST2", receipt_ids: ["a".repeat(64)] });
  assert.equal(app.panel.hasPending(), true); app.click("Open PDF"); await until(() => app.pdfs.length); assert.deepEqual(app.pdfs, [{ scope: "users/0", attachment_key: "PDFTEST2" }]);
});

test("retry uses only a backend-admitted receipt and never runs during refresh", async (t) => {
  const app = harness(t, { state: { records: [receipt({ status: "waiting_to_send", actions: { check: true, retry: true, discard: true } })] } });
  await app.show(); assert.equal(app.calls.some((row) => row.name === "zotero/annotation-retry"), false);
  app.click("Retry saved changes"); await until(() => app.updates.length);
  assert.deepEqual(app.calls.find((row) => row.name === "zotero/annotation-retry").body.receipt_ids, ["a".repeat(64)]);
});

test("orphan operations remain distinct across pages and can only check saved state", async (t) => {
  const first = receipt({ receipt_id: null, operation_ids: ["b".repeat(64)], actions: { check: true, retry: true, discard: true } });
  const second = receipt({ receipt_id: null, operation_ids: ["c".repeat(64)], actions: { check: true, retry: true, discard: true } });
  const app = harness(t, { state: { records: [first], next: 32 }, api: (name, body) => {
    if (name === "zotero/annotation-recovery" && body.start === 32) return { records: [first, second], server_id: "SYNTHETIC001", next_start: null };
  } });
  await app.show(); app.click("Load more saved changes"); await until(() => app.container.querySelectorAll(".annotation-recovery-list .annotation-change").length === 2);
  assert.equal(app.byText("Retry saved changes"), undefined); assert.equal(app.byText("Keep Zotero version"), undefined);
  app.click("Check saved state"); await until(() => app.updates.length);
  assert.deepEqual(app.calls.find((row) => row.name === "zotero/annotation-reconcile").body, { scope: "users/0", attachment_key: "PDFTEST2", operation_ids: ["b".repeat(64)] });
  assert.equal(app.calls.some((row) => row.name === "zotero/annotation-retry" || row.name === "zotero/annotation-discard"), false);
});

test("discard requires a review confirmation and preserves selected local content", async (t) => {
  const app = harness(t, { state: { records: [receipt({ status: "conflict", observed: [{ id: "ANNTEST2", state: "conflict", version: 8 }], actions: { check: true, retry: false, discard: true } })] } });
  await app.show(); app.click("Keep Zotero version");
  assert.equal(app.calls.some((row) => row.name === "zotero/annotation-discard"), false);
  assert.match(app.container.querySelector(".annotation-review").textContent, /does not delete/);
  app.click("Cancel"); assert.ok(app.container.querySelector(".annotation-review").hidden);
  app.click("Keep Zotero version"); app.click("Keep Zotero version and retain local draft"); await until(() => app.updates.length);
  assert.deepEqual(app.calls.find((row) => row.name === "zotero/annotation-discard").body, { scope: "users/0", attachment_key: "PDFTEST2", receipt_ids: ["a".repeat(64)], discard: true });
  assert.match(app.container.querySelector("textarea").value, /Personal interpretation/);
});

test("clipboard fallback selects retained content for ordinary keyboard copy", async (t) => {
  const app = harness(t); await app.show(); app.click("Copy local draft");
  await until(() => app.container.querySelector('[role="status"]').textContent.includes("draft is selected"));
  const value = app.container.querySelector("textarea"); assert.equal(app.window.document.activeElement, value); assert.equal(value.selectionStart, 0); assert.equal(value.selectionEnd, value.value.length);
});

test("confirmed Zotero saves and note updates are displayed as separate states", async (t) => {
  const app = harness(t, { state: { records: [receipt({ status: "saved", actions: { check: false, retry: false, discard: false } })], jobs: [job({ state: "waiting_for_draft", reason: "Save or discard the current draft." })] } });
  await app.show(); assert.match(app.container.textContent, /Saved in Zotero/); assert.match(app.container.textContent, /Note update waiting for your draft/);
  assert.equal(app.byText("Review annotation section"), undefined); assert.equal(app.panel.hasPending(), true);
});

test("projection choices use reviewed identities and do not accept changes into an unsaved note", async (t) => {
  const app = harness(t, { state: { jobs: [job()], document: { id: "note-one", revision: "revision-one", dirty: true } } });
  await app.show(); app.click("Review annotation section"); await until(() => app.messages.length);
  assert.equal(app.calls.some((row) => row.name === "zotero/annotation-projection-preview"), false);
  app.state.document.dirty = false; app.click("Review annotation section"); await until(() => !app.container.querySelector(".annotation-review").hidden);
  assert.equal(app.container.querySelector('[aria-label="Current annotation section"]').value, "My edited section");
  assert.equal(app.container.querySelector('[aria-label="Proposed Zotero section"]').value, "Confirmed Zotero quotation");
  app.click("Keep edits as personal prose and rebuild"); await until(() => app.updates.length);
  assert.deepEqual(app.calls.find((row) => row.name === "zotero/annotation-projection-resolve").body, { workspace_id: "workspace-one", note_path: "notes/source.md", document_id: "note-one", revision: "revision-one", scope: "users/0", attachment_key: "PDFTEST2", preview_token: "review-token", choice: "preserve" });
});

test("stale projection previews cannot overwrite a changed note", async (t) => {
  const app = harness(t, { state: { jobs: [job()] } }); await app.show(); app.click("Review annotation section");
  await until(() => !app.container.querySelector(".annotation-review").hidden); app.state.document.revision = "new-external-revision";
  app.click("Rebuild and keep a recovery copy"); await until(() => app.messages.some((row) => row[0].includes("note changed")));
  assert.equal(app.calls.some((row) => row.name === "zotero/annotation-projection-resolve"), false); assert.equal(app.panel.hasBusy(), false); assert.equal(app.container.querySelector(".annotation-review").hidden, false);
});

test("pagination rejects a changed connection and failed refresh preserves pending changes", async (t) => {
  let fail = false;
  const app = harness(t, { state: { next: 32 }, api: (name, body) => {
    if (name === "zotero/annotation-recovery" && body.start === 32) return { records: [], server_id: "DIFFERENT001" };
    if (name === "zotero/annotation-recovery" && fail) throw new Error("Zotero is closed");
  } });
  await app.show(); app.click("Load more saved changes"); await until(() => app.messages.length);
  assert.match(app.messages[0][0], /connection changed/); assert.equal(app.container.querySelectorAll("[data-receipt-id]").length, 1);
  fail = true; await app.panel.refresh(); assert.equal(app.container.querySelectorAll("[data-receipt-id]").length, 1); assert.match(app.container.querySelector('[role="status"]').textContent, /closed/);
});

test("Escape closes the review first, then the panel and restores trigger focus", async (t) => {
  const app = harness(t, { state: { records: [receipt({ status: "conflict", actions: { discard: true } })] } });
  const trigger = app.window.document.getElementById("trigger"); trigger.focus(); await app.show(); app.click("Keep Zotero version");
  app.container.dispatchEvent(new app.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true })); assert.ok(app.container.querySelector(".annotation-review").hidden); assert.equal(app.container.hidden, false);
  app.container.dispatchEvent(new app.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true })); assert.ok(app.container.hidden); assert.equal(app.window.document.activeElement, trigger);
});

test("destroy prevents late read results from rebuilding a detached panel", async (t) => {
  let release;
  const app = harness(t, { api: (name) => name === "zotero/annotation-recovery" ? new Promise((resolve) => { release = resolve; }) : undefined });
  app.panel.show(); await until(() => release); app.panel.destroy(); release({ records: [receipt()], next_start: null, server_id: "SYNTHETIC001" });
  await new Promise((resolve) => setTimeout(resolve, 10)); assert.equal(app.container.childNodes.length, 0);
});
