import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { JSDOM, VirtualConsole } from "jsdom";

const source = readFileSync(new URL("../../src/auto_zettelkasten/web/workspace-library.js", import.meta.url), "utf8");
const record = (key, data, version = 1) => ({ key, version, data: { key, version, ...data } });
const page = (records, options = {}) => ({ records, start: 0, limit: 64, total: records.length, next_start: null, library_version: 7, server_id: "SYNTHETIC001", ...options });
async function until(check, label = "condition") {
  for (let i = 0; i < 150; i++) { if (check()) return; await new Promise((resolve) => setTimeout(resolve, 5)); }
  assert.fail(`Timed out waiting for ${label}`);
}
function harness(t, options = {}) {
  const errors = []; const console = new VirtualConsole(); console.on("jsdomError", (error) => errors.push(error));
  const dom = new JSDOM('<div id="panel"></div>', { runScripts: "dangerously", url: "http://127.0.0.1:8765", pretendToBeVisual: true, virtualConsole: console });
  const window = dom.window; const container = window.document.getElementById("panel");
  const calls = [], notes = [], pdfs = [], migrations = [], notices = [];
  const item = record("ITEM0001", { itemType: "journalArticle", title: "Conflict and institutions", date: "2021", publicationTitle: "Research", tags: [{ tag: "generated", type: 1 }, { tag: "manual" }], collections: ["COLL0001", "UNSEEN01"] });
  const state = {
    status: { connected: true, writable: true, writes_available: true },
    collections: page([record("COLL0001", { name: "Research", parentCollection: false }), record("COLL0002", { name: "Methods", parentCollection: "COLL0001" })]),
    items: page([{ ...item, note_path: "notes/stable.md" }]),
    children: page([{ ...record("PDF00001", { title: "Article PDF", itemType: "attachment", contentType: "application/pdf", parentItem: "ITEM0001" }), note_path: "notes/stable.md" }]),
    ...options.state,
  };
  window.eval(source);
  const panel = window.createZoteroLibraryPanel({ container,
    api: async (name, body) => {
      calls.push({ name, body: JSON.parse(JSON.stringify(body)) });
      const override = await options.api?.(name, body, state); if (override !== undefined) return override;
      if (name === "zotero/status") return structuredClone(state.status);
      if (name === "zotero/libraries") return { libraries: [{ scope: "users/0", name: "My Library", writable: state.status.writable }, { scope: "groups/123", name: "Group", writable: false }] };
      if (name === "zotero/collections") return structuredClone(state.collections);
      if (name === "zotero/items") return structuredClone(state.items);
      if (name === "zotero/children") return structuredClone(state.children);
      if (name === "zotero/authorize") { state.status.writable = true; return { authorized: true }; }
      if (["zotero/item-update", "zotero/collection-create", "zotero/collection-rename"].includes(name)) return { saved: true };
      throw new Error(`Unexpected ${name}`);
    },
    getWorkspace: () => ({ id: "workspace-one" }),
    onOpenNote: (path) => notes.push(path), onOpenPDF: (value) => pdfs.push(JSON.parse(JSON.stringify(value))),
    onMigrate: (value) => migrations.push(JSON.parse(JSON.stringify(value))), message: (...value) => notices.push(value),
  });
  const byText = (label) => [...container.querySelectorAll("button")].find((button) => button.textContent === label);
  const click = (label) => { const button = byText(label); assert.ok(button, label); button.click(); };
  const open = async () => { panel.setVisible(true); await until(() => container.querySelector('[data-item-key="ITEM0001"]'), "initial library"); };
  const select = async () => { container.querySelector('[data-item-key="ITEM0001"]').click(); await until(() => container.querySelector(".zotero-open-pdf"), "item children"); };
  t.after(() => { panel.destroy(); window.close(); assert.deepEqual(errors, []); });
  return { window, container, panel, calls, state, notes, pdfs, migrations, notices, click, byText, open, select };
}

test("library navigation shares stable notes and attachment identities without copying content", async (t) => {
  const app = harness(t); assert.equal(app.calls.length, 0);
  await app.open();
  assert.equal(app.container.querySelector('[data-collection-key="COLL0002"]').dataset.depth, "1");
  await app.select(); app.click("Open atomic note"); app.click("Open PDF: Article PDF");
  await until(() => app.notes.length && app.pdfs.length);
  assert.deepEqual(app.notes, ["notes/stable.md"]);
  assert.deepEqual(app.pdfs[0], { scope: "users/0", attachment_key: "PDF00001", item_key: "ITEM0001", note_path: "notes/stable.md" });
  assert.equal(app.calls.find((call) => call.name === "zotero/children").body.workspace_id, "workspace-one");
  assert.equal(app.calls.filter((call) => /authorize|update|create|rename/.test(call.name)).length, 0);
  app.click("Link this atomic note…"); await until(() => app.migrations.length);
  assert.deepEqual(app.migrations[0], { scope: "users/0", item_key: "ITEM0001", note_path: "notes/stable.md" }, "migration does not guess among PDFs");
});

test("read-only deployment exposes browsing but no authorization or writers", async (t) => {
  const app = harness(t, { state: { status: { connected: true, writable: false, writes_available: false } } });
  await app.open(); await app.select();
  assert.ok(app.byText("Enable editing").hidden); assert.ok(app.byText("New").hidden);
  assert.equal(app.byText("Edit item and collections"), undefined); assert.equal(app.byText("Link this atomic note…"), undefined);
  app.byText("Enable editing").click(); await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(app.calls.filter((call) => call.name === "zotero/authorize").length, 0);
});

test("metadata edits preserve unlisted collection memberships and retained tag ownership", async (t) => {
  const app = harness(t); await app.open(); await app.select(); app.click("Edit item and collections");
  await until(() => app.container.querySelector("form.zotero-library-form"));
  const form = app.container.querySelector("form.zotero-library-form");
  form.elements.title.value = "A personal bibliographic correction";
  form.elements.tags.value = "generated\nnew tag\nnew tag";
  const member = form.querySelector('input[type="checkbox"][value="COLL0001"]'); member.checked = false; member.dispatchEvent(new app.window.Event("change"));
  form.dispatchEvent(new app.window.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => app.calls.some((call) => call.name === "zotero/item-update"));
  const body = app.calls.find((call) => call.name === "zotero/item-update").body;
  assert.deepEqual(body.changes, { title: "A personal bibliographic correction", tags: [{ tag: "generated", type: 1 }, { tag: "new tag" }], collections: ["UNSEEN01"] });
  assert.equal(body.expected_version, 1); assert.equal(body.scope, "users/0"); assert.match(body.write_token, /^[a-f0-9]{32}$/);
});

test("uncertain writes preserve entered text and do not retry", async (t) => {
  const app = harness(t, { api: (name) => { if (name === "zotero/item-update") throw Object.assign(new Error("Unknown write outcome"), { code: "write_uncertain" }); } });
  await app.open(); await app.select(); app.click("Edit item and collections");
  await until(() => app.container.querySelector("form.zotero-library-form"));
  const form = app.container.querySelector("form.zotero-library-form"); form.elements.title.value = "Pending title";
  const submit = () => form.dispatchEvent(new app.window.Event("submit", { bubbles: true, cancelable: true })); submit();
  await until(() => form.querySelector(".zotero-form-error").textContent.includes("No automatic retry"));
  assert.equal(form.elements.title.value, "Pending title"); assert.equal(form.querySelector('button[type="submit"]').disabled, true);
  submit(); await app.panel.refresh();
  assert.equal(app.calls.filter((call) => call.name === "zotero/item-update").length, 1);
});

test("failed refresh keeps loaded items and a form draft blocks polling", async (t) => {
  let offline = false;
  const app = harness(t, { api: (name) => { if (offline && name === "zotero/collections") throw new Error("Incomplete inventory"); } });
  await app.open(); await app.select(); offline = true; await app.panel.refresh();
  assert.ok(app.container.querySelector('[data-item-key="ITEM0001"]'));
  assert.match(app.container.querySelector('[role="status"]').textContent, /Incomplete inventory/);
  app.click("Edit item and collections"); await until(() => app.panel.hasDraft());
  const count = app.calls.length; await app.panel.refresh(); assert.equal(app.calls.length, count);
});

test("paging refuses changed versions and merges duplicate identities only once", async (t) => {
  const app = harness(t, { state: { items: page([{ ...record("ITEM0001", { title: "First", itemType: "book" }), note_path: "notes/stable.md" }], { total: 2, next_start: 1 }) }, api: (name, body) => {
    if (name === "zotero/items" && body.start === 1) return page([record("ITEM0001", { title: "First", itemType: "book" }), record("ITEM0002", { title: "Second", itemType: "book" })], { total: 2, library_version: 8 });
  } });
  await app.open(); app.click("Load more items");
  await until(() => app.container.querySelector('[role="status"]').textContent.includes("changed while loading"));
  assert.equal(app.container.querySelectorAll(".zotero-item-row").length, 1);
});

test("read-only Zotero HTML notes do not execute or display unsafe markup", async (t) => {
  const app = harness(t, { state: { children: page([record("PDF00001", { title: "Article PDF", itemType: "attachment", contentType: "application/pdf", parentItem: "ITEM0001" }), record("NOTE0001", { itemType: "note", note: '<p>Safe quotation</p><script>window.compromised=true</script><img src=x onerror="window.compromised=true"><iframe src="https://example.invalid"></iframe>' })]) } });
  await app.open(); await app.select();
  const note = app.container.querySelector(".zotero-html-note"); assert.match(note.textContent, /Safe quotation/);
  assert.doesNotMatch(note.textContent, /compromised/); assert.equal(note.querySelector("script,img,iframe"), null); assert.equal(app.window.compromised, undefined);
});

test("new collection has explicit parent and cancellation never writes", async (t) => {
  const app = harness(t); await app.open(); app.click("Research"); await until(() => app.container.querySelector('[data-collection-key="COLL0001"]').getAttribute("aria-current") === "true");
  app.click("New"); await until(() => app.panel.hasDraft()); app.click("Cancel"); await until(() => !app.panel.hasDraft());
  assert.equal(app.calls.some((call) => call.name === "zotero/collection-create"), false);
  app.click("New"); await until(() => app.panel.hasDraft());
  const form = app.container.querySelector("form.zotero-library-form"); form.elements.name.value = "New subcollection";
  form.dispatchEvent(new app.window.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => app.calls.some((call) => call.name === "zotero/collection-create"));
  const body = app.calls.find((call) => call.name === "zotero/collection-create").body;
  assert.equal(body.parent_key, "COLL0001"); assert.equal(body.name, "New subcollection"); assert.match(body.key, /^[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}$/);
});

test("outdated item response cannot replace a later selection", async (t) => {
  let release;
  const app = harness(t, { state: { items: page([record("ITEM0001", { itemType: "book", title: "First" }), record("ITEM0002", { itemType: "book", title: "Second" })]) }, api: (name, body) => {
    if (name === "zotero/children" && body.parent_key === "ITEM0001") return new Promise((resolve) => { release = resolve; });
    if (name === "zotero/children" && body.parent_key === "ITEM0002") return page([]);
  } });
  await app.open(); app.container.querySelector('[data-item-key="ITEM0001"]').click();
  await until(() => release); app.container.querySelector('[data-item-key="ITEM0002"]').click();
  await until(() => app.container.querySelector(".zotero-library-details h3").textContent === "Second");
  release(page([record("PDF00001", { title: "Wrong stale PDF", itemType: "attachment", contentType: "application/pdf", parentItem: "ITEM0001" })]));
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(app.container.querySelector(".zotero-library-details h3").textContent, "Second");
  assert.equal(app.container.querySelector(".zotero-open-pdf"), null);
});

test("hidden panel does not poll on focus and late reads cannot repopulate destroyed panel", async (t) => {
  let release;
  const app = harness(t); await app.open(); app.panel.setVisible(false);
  const before = app.calls.length; app.window.dispatchEvent(new app.window.Event("focus"));
  await new Promise((resolve) => setTimeout(resolve, 10)); assert.equal(app.calls.length, before);
  const empty = new JSDOM('<div id="x"></div>', { runScripts: "dangerously", url: "http://127.0.0.1:8765" });
  empty.window.eval(source); const container = empty.window.document.getElementById("x");
  const panel = empty.window.createZoteroLibraryPanel({ container, api: () => new Promise((resolve) => { release = resolve; }) });
  panel.setVisible(true); panel.destroy(); release({ connected: true });
  await new Promise((resolve) => setTimeout(resolve, 10)); assert.equal(container.childNodes.length, 0); empty.window.close();
});

test("ambiguous note identities are offered explicitly rather than reported absent or guessed", async (t) => {
  const app = harness(t, { state: { items: page([{ ...record("ITEM0001", { itemType: "book", title: "Multiple source summaries" }), note_candidates: [{ path: "notes/a.md", title: "Same title" }, { path: "notes/b.md", title: "Same title" }] }]) } });
  await app.open(); await app.select();
  assert.match(app.container.querySelector(".zotero-item-row").textContent, /2 atomic notes/);
  assert.doesNotMatch(app.container.querySelector(".zotero-library-details").textContent, /No atomic note yet/);
  assert.equal(app.notes.length, 0); app.click("Open Same title (notes/b.md)");
  await until(() => app.notes.length); assert.deepEqual(app.notes, ["notes/b.md"]);
});

test("historical key-only notes stay unbound until an explicit scoped source action", async (t) => {
  const app = harness(t, { state: { status: { connected: true, writable: false, writes_available: false }, items: page([{ ...record("ITEM0001", { itemType: "book", title: "Historical source" }), unbound_note_candidates: [
    { path: "notes/legacy-a.md", title: "Same title", item_key: "ITEM0001", requires_scope_confirmation: true },
    { path: "notes/legacy-b.md", title: "Same title", item_key: "ITEM0001", requires_scope_confirmation: true },
  ] }]) } });
  await app.open(); await app.select();
  assert.match(app.container.querySelector(".zotero-item-row").textContent, /Existing note needs source binding/);
  assert.doesNotMatch(app.container.querySelector(".zotero-library-details").textContent, /No atomic note yet/);
  assert.equal(app.notes.length, 0); assert.equal(app.pdfs.length, 0); assert.equal(app.migrations.length, 0);
  app.click("Open Same title (notes/legacy-a.md)"); await until(() => app.notes.length);
  assert.deepEqual(app.notes, ["notes/legacy-a.md"]); assert.equal(app.pdfs.length, 0);
  app.click("Check source in this library: Same title (notes/legacy-b.md)"); await until(() => app.pdfs.length);
  assert.deepEqual(app.pdfs[0], { scope: "users/0", item_key: "ITEM0001", note_path: "notes/legacy-b.md", confirm_scope: true });
  assert.equal(app.migrations.length, 0); assert.equal(app.calls.some((call) => /authorize|update|create|rename/.test(call.name)), false);
});
