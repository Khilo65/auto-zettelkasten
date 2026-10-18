import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { JSDOM, VirtualConsole } from "jsdom";

const asset = (name) => readFileSync(new URL(`../../src/auto_zettelkasten/web/${name}`, import.meta.url), "utf8");

function harness(t, desktop = "true") {
  const errors = [], virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (error) => errors.push(error));
  let html = asset("index.html").replace("__SESSION_TOKEN__", "zoom-test-token").replace("__DESKTOP_SHELL__", desktop);
  if (desktop === null) html = html.replace(/<meta name="desktop-shell"[^>]*>/, "");
  const dom = new JSDOM(html, { url: "http://127.0.0.1:8765", runScripts: "dangerously", pretendToBeVisual: true, virtualConsole });
  const w = dom.window, requests = [];
  w.fetch = (url, options) => new Promise((resolve) => {
    requests.push({ url, options, body: JSON.parse(options.body), resolve });
  });
  const script = w.document.createElement("script");
  script.textContent = asset("app.js");
  w.document.body.append(script);
  const el = (id) => w.document.getElementById(id);
  const key = (key, options = {}, target = el("workspace-quick-query")) => {
    const event = new w.KeyboardEvent("keydown", { bubbles: true, cancelable: true, metaKey: true, key, ...options });
    target.dispatchEvent(event);
    return event;
  };
  const respond = async (index, result = { level: requests[index].body.level }, status = 200) => {
    assert.ok(requests[index], `Request ${index} exists`);
    requests[index].resolve({ ok: status === 200, status, json: async () => result });
    await new Promise((resolve) => setTimeout(resolve, 0));
  };
  t.after(() => { w.close(); assert.deepEqual(errors, []); });
  return { w, el, key, requests, respond };
}

test("desktop zoom uses authenticated absolute levels in note fields, dialogs and the mapper", async (t) => {
  const app = harness(t);
  assert.equal(app.requests.length, 0, "Loading the app does not request or reset zoom");
  app.el("workspace-quick-dialog").open = true;
  assert.equal(app.key("+").defaultPrevented, true);
  assert.equal(app.requests[0].url, "/api/view-zoom");
  assert.equal(app.requests[0].options.headers["X-Session-Token"], "zoom-test-token");
  assert.equal(app.requests[0].options.method, "POST");
  assert.deepEqual(app.requests[0].body, { level: 1.1 });
  await app.respond(0);
  assert.equal(app.el("workspace-message").hidden, true);
  app.key("=", { shiftKey: false }); await app.respond(1);
  app.key("Add", { metaKey: false, ctrlKey: true, code: "NumpadAdd" }); await app.respond(2);
  app.el("workspace-screen").hidden = true;
  app.key("-", {}, app.el("output-folder")); await app.respond(3);
  app.key("Subtract", { metaKey: false, ctrlKey: true, code: "NumpadSubtract" }); await app.respond(4);
  app.key("0"); await app.respond(5);
  app.key("0"); await app.respond(6);
  assert.deepEqual(app.requests.map(({ body }) => body.level), [1.1, 1.2, 1.3, 1.2, 1.1, 1, 1]);
});

test("rapid zoom keys serialize requests and coalesce only the latest bounded level", async (t) => {
  const app = harness(t);
  app.key("+");
  for (let i = 0; i < 40; i += 1) app.key("+");
  assert.equal(app.requests.length, 1);
  await app.respond(0);
  assert.deepEqual(app.requests.map(({ body }) => body.level), [1.1, 2]);
  await app.respond(1);
  app.key("+");
  assert.equal(app.requests.length, 2, "The upper bound makes no redundant request");
  for (let i = 0; i < 40; i += 1) app.key("-");
  assert.deepEqual(app.requests[2].body, { level: 1.9 });
  await app.respond(2);
  assert.deepEqual(app.requests[3].body, { level: 0.5 });
  await app.respond(3);
  app.key("-");
  assert.equal(app.requests.length, 4, "The lower bound makes no redundant request");
});

test("reset after an in-flight zoom restores 100 percent without a stale response", async (t) => {
  const app = harness(t);
  app.key("+"); app.key("+"); app.key("0");
  assert.equal(app.requests.length, 1);
  await app.respond(0);
  assert.deepEqual(app.requests[1].body, { level: 1 });
  await app.respond(1);
  app.key("-");
  assert.deepEqual(app.requests[2].body, { level: 0.9 });
  await app.respond(2);
});

test("an older failed request cannot roll back newer key intent at the same level", async (t) => {
  const app = harness(t);
  app.key("+"); app.key("+"); app.key("-");
  await app.respond(0, { error: "The window was busy." }, 500);
  assert.equal(app.requests.length, 2);
  assert.deepEqual(app.requests[1].body, { level: 1.1 });
  await app.respond(1);
  assert.equal(app.el("workspace-message").hidden, true);
  app.key("+");
  assert.deepEqual(app.requests[2].body, { level: 1.2 });
  await app.respond(2);
});

test("failed or malformed zoom responses keep the confirmed scale and surface recoverable errors", async (t) => {
  const app = harness(t);
  app.key("+"); await app.respond(0);
  app.key("+"); await app.respond(1, { error: "Window unavailable." }, 500);
  assert.equal(app.el("workspace-message").hidden, false);
  assert.match(app.el("workspace-message").textContent, /Window unavailable/);
  app.key("+");
  assert.deepEqual(app.requests[2].body, { level: 1.2 });
  app.el("workspace-message").textContent = "A note has unsaved changes.";
  await app.respond(2);
  assert.equal(app.el("workspace-message").textContent, "A note has unsaved changes.");
  assert.equal(app.el("workspace-message").hidden, false);
  app.el("workspace-screen").hidden = true;
  app.key("+"); await app.respond(3, { level: 99 });
  assert.equal(app.el("notice").hidden, false);
  assert.match(app.el("notice").textContent, /view size/i);
  app.key("+");
  assert.deepEqual(app.requests[4].body, { level: 1.3 });
  await app.respond(4);
  assert.equal(app.el("notice").hidden, true);
});

test("only desktop zoom chords are captured, preserving typing, other shortcuts, wheel and pinch", async (t) => {
  const app = harness(t), target = app.el("workspace-quick-query"), received = [];
  target.addEventListener("keydown", (event) => received.push(event.key));
  for (const [key, options] of [
    ["+", { metaKey: false }], ["-", { metaKey: false }], ["0", { metaKey: false }],
    ["o", {}], ["s", {}], ["n", {}], ["b", {}],
    ["+", { altKey: true }], ["+", { isComposing: true }],
  ]) assert.equal(app.key(key, options).defaultPrevented, false);
  assert.equal(received.length, 9);
  assert.equal(app.requests.length, 0);
  app.key("+");
  assert.equal(received.length, 9, "Zoom is handled before graph or editor key handlers");
  const wheel = new app.w.WheelEvent("wheel", { bubbles: true, cancelable: true, ctrlKey: true });
  target.dispatchEvent(wheel);
  assert.equal(wheel.defaultPrevented, false);
  await app.respond(0);
  for (const desktop of ["false", null]) {
    const browser = harness(t, desktop);
    for (const key of ["+", "=", "-", "0"]) assert.equal(browser.key(key).defaultPrevented, false);
    assert.equal(browser.requests.length, 0);
  }
});
