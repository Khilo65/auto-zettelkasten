import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { JSDOM } from "jsdom";

function harness(onPreview) {
  const window = new JSDOM('<div id="readers"></div>', { url: "http://127.0.0.1:65432/", runScripts: "outside-only" }).window;
  window.eval(readFileSync(new URL("../../src/auto_zettelkasten/web/workspace-source-reader.js", import.meta.url), "utf8"));
  const apiCalls = [], messages = [], packets = [];
  const config = { id: "users/0:PDFKEY01", scope: "users/0", attachment_key: "PDFKEY01", item_key: "SOURCE01", title: "Synthetic PDF", url: "/api/zotero/attachments/synthetic-capability/file", readOnly: true,
    preview_preferences: { ANNOT001: false }, annotations: [{ id: "ANNOT001", type: "highlight", version: 2, text: "Confirmed quote", position: { pageIndex: 0, rects: [[1, 2, 3, 4]] } }] };
  const manager = window.createSourceReader({ container: window.document.getElementById("readers"), api: async (...args) => { apiCalls.push(args); return {}; }, message: (...args) => messages.push(args), onPreview });
  const boundConfig = manager.register(config); manager.activate(config.id);
  const frame = window.document.querySelector("iframe"), instanceId = new URL(frame.src).hash.slice("#instance=".length);
  frame.contentWindow.postMessage = data => packets.push(data);
  const emit = (type, data = {}, overrides = {}) => window.dispatchEvent(new window.MessageEvent("message", { source: frame.contentWindow, origin: window.location.origin, data: { channel: "az-zotero-reader-v1", instanceId, type, ...data }, ...overrides }));
  emit("ready"); emit("initialized");
  const settle = () => new Promise(resolve => setImmediate(resolve));
  return { window, manager, config: boundConfig, frame, emit, settle, apiCalls, messages, packets };
}

test("source reader admits only exact confirmed preview identities without setting native pending state", async () => {
  const calls = [];
  const h = harness(async (...args) => calls.push(args));
  assert.deepEqual(JSON.parse(JSON.stringify(h.packets.find(row => row.type === "init").previewPreferences)), { ANNOT001: false });
  h.emit("preview-request", { annotationId: "ANNOT001", annotationVersion: 2, enabled: true });
  await h.settle();
  assert.equal(calls.length, 1); assert.equal(calls[0][0].attachment_key, "PDFKEY01"); assert.equal(calls[0][1].text, "Confirmed quote"); assert.equal(calls[0][2], true);
  assert.equal(h.manager.hasPending(), false); assert.equal(h.apiCalls.length, 0);
  assert.equal(h.packets.at(-1).type, "preview-result"); assert.equal(h.packets.at(-1).success, true);
  assert.equal(h.config.preview_preferences.ANNOT001, true);
  h.window.close();
});

test("source reader rejects forged, stale, unsupported and unrequested preview decisions", async () => {
  let calls = 0;
  const h = harness(async () => { calls += 1; });
  for (const fields of [
    { annotationId: "UNKNOWN1", annotationVersion: 2, enabled: true },
    { annotationId: "ANNOT001", annotationVersion: 1, enabled: true },
    { annotationId: "ANNOT001", annotationVersion: 2, enabled: "true" },
  ]) h.emit("preview-request", fields);
  h.emit("preview-request", { annotationId: "ANNOT001", annotationVersion: 2, enabled: true }, { source: null });
  await h.settle();
  assert.equal(calls, 0); assert.equal(h.apiCalls.length, 0); assert.equal(h.manager.hasPending(), false);
  assert.equal(h.packets.filter(row => row.type === "preview-result" && row.success === false).length, 3);
  h.window.close();
});

test("preview failures stay separate from native annotation recovery and never request a write retry", async () => {
  const h = harness(async () => { throw new Error("Verified PDF is unavailable."); });
  h.emit("preview-request", { annotationId: "ANNOT001", annotationVersion: 2, enabled: true });
  await h.settle();
  assert.equal(h.manager.hasPending(), false); assert.equal(h.manager.hasFailed(h.config.id), false); assert.equal(h.apiCalls.length, 0);
  assert.equal(h.packets.at(-1).type, "preview-result"); assert.equal(h.packets.at(-1).success, false);
  assert.match(h.messages.at(-1)[0], /Annotation image: Verified PDF is unavailable/);
  h.window.close();
});
