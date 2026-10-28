import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { JSDOM, VirtualConsole } from "jsdom";

const asset = name => readFileSync(new URL(`../../src/auto_zettelkasten/web/${name}`, import.meta.url), "utf8");
const scripts = ["workspace-vendor.js", "app.js", "workspace.js"].map(asset);

async function until(check, description) {
  const deadline = Date.now() + 3000;
  while (!check()) {
    if (Date.now() > deadline) assert.fail(`Timed out: ${description}`);
    await new Promise(resolve => setTimeout(resolve, 5));
  }
}

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

function harness(t, options = {}) {
  const errors = [], requests = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", error => errors.push(error));
  const dom = new JSDOM(asset("index.html"), {
    url: "http://127.0.0.1:8765/", runScripts: "dangerously", pretendToBeVisual: true, virtualConsole,
  });
  const window = dom.window;
  const pollers = [];
  const setInterval = window.setInterval.bind(window);
  window.setInterval = (callback, milliseconds, ...args) => milliseconds === 5000 ? (pollers.push(callback), 0) : setInterval(callback, milliseconds, ...args);
  window.HTMLElement.prototype.scrollIntoView = function () {};
  window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  window.Range.prototype.getClientRects = () => [];
  window.Range.prototype.getBoundingClientRect = () => ({ left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 });
  window.document.elementFromPoint = () => window.document.querySelector(".cm-content") || window.document.body;
  const text = options.text || "# Start\n\nA substantive paragraph.\n";
  const disk = { id: "note-1", path: "Start.md", name: "Start.md", text, revision: "r1", editable: true,
    reason: "", bom: false, newline: "lf", recovery_key: "image-synthetic-note" };
  const workspace = { id: "workspace-1", name: "Notes", path: "/synthetic", editable: true, truncated: false,
    entries: [{ path: "Start.md", name: "Start.md", kind: "file", editable: true },
      { path: "diagram.png", name: "diagram.png", kind: "asset", editable: false, media_type: "image" }] };
  const media = (path = "diagram.png") => ({ path, relative_path: path, mime: "image/png", revision: "image-r1",
    width: 20, height: 10, size: 100, url: "/api/workspace/media/" + "a".repeat(43) });
  window.fetch = async (url, request = {}) => {
    const importing = url.startsWith("/api/workspace/import-image?");
    const body = importing ? request.body : request.body ? JSON.parse(request.body) : {};
    requests.push({ url, body });
    let status = 200, result;
    if (importing) {
      const response = options.upload ? await options.upload(body) : { asset: media("attachments/images/imported.png"), workspace };
      status = response.status || 200; result = response.result || response;
    } else if (url === "/api/workspace/open" || url === "/api/workspace/tree") result = { workspace };
    else if (url === "/api/workspace/index") result = { documents: [{ ...disk }], relations: [], warnings: [], truncated: false };
    else if (url === "/api/workspace/read") result = { document: { ...disk } };
    else if (url === "/api/workspace/media-resolve") result = { asset: media(body.target.replace(/^\.\//, "")) };
    else if (url === "/api/workspace/media-open") result = { asset: media(body.path) };
    else if (url === "/api/zotero/open-source") result = { warning: "Synthetic PDF locator recorded.", choices: [] };
    else if (url === "/api/workspace/save") {
      disk.text = body.text; disk.revision = "saved-r2"; result = { document: { ...disk } };
    } else throw new Error(`Unexpected synthetic image request: ${url}`);
    return { ok: status === 200, status, json: async () => structuredClone(result) };
  };
  for (const source of scripts) {
    const script = window.document.createElement("script"); script.textContent = source; window.document.body.append(script);
  }
  t.after(() => {
    window.close();
    assert.deepEqual(errors, [], "No uncaught workspace or CodeMirror exceptions");
  });
  const element = id => window.document.getElementById(id);
  const click = id => element(id).click();
  const view = () => window.WorkspaceKit.EditorView.findFromDOM(window.document.querySelector(".cm-editor"));
  const openFolder = async () => {
    click("workspace-welcome-open"); element("workspace-folder-path").value = "/synthetic";
    element("workspace-folder-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    await until(() => window.document.querySelector('.tree-row[data-path="Start.md"]'), "synthetic tree");
  };
  const open = async () => {
    await openFolder(); window.document.querySelector('.tree-row[data-path="Start.md"]').click();
    await until(() => window.document.querySelector(".cm-editor"), "actual CodeMirror editor");
    return view();
  };
  const mode = value => window.document.querySelector(`[data-mode="${value}"]`).click();
  const file = () => new window.File(["synthetic image bytes"], "picture.png", { type: "image/png" });
  const paste = () => {
    const event = new window.Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", { value: { files: [file()] } });
    view().contentDOM.dispatchEvent(event); assert.equal(event.defaultPrevented, true);
  };
  return { window, disk, requests, workspace, media, element, click, view, openFolder, open, mode, file, paste, poll: () => pollers[0]?.() };
}

test("Reading images rehydrate after background workspace polling", async t => {
  const app = harness(t, { text: "# Start\n\n![Figure](diagram.png)\n" });
  await app.open(); app.mode("reading");
  await until(() => app.window.document.querySelector("#workspace-reader .markdown-image img"), "initial Reading image");
  const before = app.window.document.querySelector("#workspace-reader .markdown-image img");
  await app.poll();
  await until(() => app.window.document.querySelector("#workspace-reader .markdown-image img"), "Reading image after background render");
  assert.notEqual(app.window.document.querySelector("#workspace-reader .markdown-image img"), before);
});

test("image insertion preserves frontmatter in Reading and initially focused Live Preview", async t => {
  const text = '---\nsource_id: stable\ntags: [personal]\n---\n\n# Start\n\nA substantive paragraph.\n';
  for (const mode of ["reading", "live"]) {
    const app = harness(t, { text }); await app.open(); app.mode(mode);
    const input = app.element("workspace-import-image").nextElementSibling;
    Object.defineProperty(input, "files", { configurable: true, value: [app.file()] });
    input.dispatchEvent(new app.window.Event("change", { bubbles: true }));
    await until(() => app.view().state.doc.toString().includes("![picture.png]"), `${mode} body image`);
    const changed = app.view().state.doc.toString();
    assert.ok(changed.startsWith('---\nsource_id: stable\ntags: [personal]\n---\n'));
    assert.equal(app.window.WorkspaceKit.inspectProperties(changed).rows.find(row => row.key === "source_id").value, "stable");
    assert.equal(app.window.WorkspaceKit.undo(app.view()), true);
    assert.equal(app.view().state.doc.toString(), text);
  }
});

test("explicit image paste inside YAML properties leaves the document and assets untouched", async t => {
  const text = '---\ntitle: Original\n---\n\nA substantive paragraph.\n';
  const app = harness(t, { text }); await app.open(); app.mode("source");
  app.view().dispatch({ selection: { anchor: 12 } }); app.paste();
  await until(() => app.element("workspace-message").textContent.includes("cannot be inserted into properties"), "property insertion guard");
  assert.equal(app.view().state.doc.toString(), text);
  assert.equal(app.requests.filter(row => row.url.startsWith("/api/workspace/import-image?")).length, 0);
});

test("actual workspace renders image widgets in Live Preview and Reading, leaving Source raw", async t => {
  const text = "# Start\n\nA paragraph.\n\n![[diagram.png|320x180]]\n\n![Second](diagram.png)\n\nEnd.\n";
  const app = harness(t, { text });
  await app.open(); app.mode("source");
  assert.equal(app.view().state.doc.toString(), text);
  assert.equal(app.window.document.querySelectorAll("#workspace-editor .markdown-image img").length, 0);
  app.mode("live");
  await until(() => app.window.document.querySelectorAll("#workspace-editor .markdown-image img").length === 2, "real Live Preview image widgets");
  const image = app.window.document.querySelector("#workspace-editor .markdown-image img");
  assert.equal(image.width, 320); assert.equal(image.height, 180);
  assert.equal(app.view().state.doc.toString(), text);
  app.mode("reading");
  await until(() => app.window.document.querySelectorAll("#workspace-reader .markdown-image img").length === 2, "hydrated Reading images");
  assert.equal(app.window.document.querySelectorAll("#workspace-reader .markdown-image-source").length, 0);
  app.window.document.querySelector("#workspace-reader .markdown-image-open").click();
  assert.ok(app.window.document.querySelector('.workspace-image-viewer[aria-modal="true"]'));
  app.window.document.dispatchEvent(new app.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.equal(app.window.document.querySelector(".workspace-image-viewer"), null);
  app.mode("source"); assert.equal(app.view().state.doc.toString(), text);
});

test("file-tree image opens without creating a note buffer", async t => {
  const app = harness(t); await app.openFolder();
  assert.equal(app.window.document.querySelector(".cm-editor"), null);
  app.window.document.querySelector('.tree-row[data-path="diagram.png"]').click();
  await until(() => app.window.document.querySelector(".workspace-image-viewer img"), "standalone image viewer");
  assert.equal(app.window.document.querySelector(".cm-editor"), null);
  assert.equal(app.requests.filter(row => row.url === "/api/workspace/read").length, 0);
  assert.equal(app.requests.filter(row => row.url === "/api/workspace/media-open").length, 1);
  app.window.document.querySelector(".workspace-image-viewer").click();
  assert.equal(app.window.document.querySelector(".workspace-image-viewer"), null);
});

test("annotation image actions retain each physical page and never attach a source to a manual image", async t => {
  const uri = page => `zotero://open-pdf/library/items/PDFKEY01?page=${page}&annotation=ANNOT001`;
  const text = "# Start\n\n<!-- auto-zettelkasten:annotations:start -->\n## Highlights and annotations\n\n"
    + `- [Highlight · p. 458](${uri(1)})\n\n  ![First page](diagram.png)\n\n  [Open in PDF](${uri(1)})\n\n`
    + `  ![Second page](diagram.png)\n\n  [Open in PDF](${uri(2)})\n\n`
    + "<!-- auto-zettelkasten:annotations:end -->\n\n## Personal prose\n\n![Manual image](diagram.png)\n";
  const app = harness(t, { text }); await app.open();
  for (const mode of ["reading", "live"]) {
    app.mode(mode);
    const container = mode === "reading" ? "#workspace-reader" : "#workspace-editor";
    await until(() => app.window.document.querySelectorAll(`${container} .markdown-image img`).length === 3, `${mode} annotation previews`);
    const buttons = [...app.window.document.querySelectorAll(`${container} .markdown-image-source`)];
    assert.equal(buttons.length, 2, `${mode}: manual images have no inferred source action`);
    const previous = app.requests.filter(row => row.url === "/api/zotero/open-source").length;
    for (const button of buttons) button.click();
    await until(() => app.requests.filter(row => row.url === "/api/zotero/open-source").length === previous + 2, `${mode} PDF link dispatch`);
    assert.deepEqual(app.requests.filter(row => row.url === "/api/zotero/open-source").slice(previous).map(row => row.body.uri), [uri(1), uri(2)], `${mode}: page-specific image locators`);
  }
});

test("paste maps an asynchronous insertion past unrelated draft edits and undoes only the image", async t => {
  const upload = deferred(); const app = harness(t, { upload: () => upload.promise });
  await app.open(); app.mode("source");
  const original = app.view().state.doc.toString();
  const at = original.indexOf("substantive");
  app.view().dispatch({ selection: { anchor: at } }); app.paste();
  await until(() => app.requests.some(row => row.url.startsWith("/api/workspace/import-image?")), "image upload admission");
  app.view().dispatch({ changes: { from: 0, insert: "Personal draft.\n" }, userEvent: "input.type" });
  upload.resolve({ asset: app.media("attachments/images/imported.png"), workspace: app.workspace });
  await until(() => app.view().state.doc.toString().includes("!["), "mapped image insertion");
  const updated = app.view().state.doc.toString();
  assert.ok(updated.startsWith("Personal draft.\n"));
  assert.match(updated, /A !\[picture\.png\]\(<attachments\/images\/imported\.png>\)substantive paragraph/);
  assert.equal(app.disk.text, original, "insertion remains an unsaved draft");
  assert.equal(app.window.WorkspaceKit.undo(app.view()), true);
  assert.equal(app.view().state.doc.toString(), "Personal draft.\n" + original);
  assert.equal(app.requests.filter(row => row.url.startsWith("/api/workspace/import-image?")).length, 1);
});

test("editing a pending selected insertion preserves the draft instead of replacing it", async t => {
  const upload = deferred(); const app = harness(t, { upload: () => upload.promise });
  await app.open(); app.mode("source"); const initial = app.view().state.doc.toString();
  const from = initial.indexOf("substantive"), to = from + "substantive".length;
  app.view().dispatch({ selection: { anchor: from, head: to } }); app.paste();
  await until(() => app.requests.some(row => row.url.startsWith("/api/workspace/import-image?")), "deferred selected upload");
  app.view().dispatch({ changes: { from, to, insert: "edited personal" }, userEvent: "input.type" });
  const draft = app.view().state.doc.toString();
  upload.resolve({ asset: app.media("attachments/images/imported.png"), workspace: app.workspace });
  await until(() => app.element("workspace-message").textContent.includes("insertion text changed"), "conflicting insertion warning");
  assert.equal(app.view().state.doc.toString(), draft);
  assert.ok(!draft.includes("![")); assert.equal(app.disk.text, initial);
});

test("failed import leaves selection and draft unchanged, and the upload control can retry", async t => {
  let uploads = 0;
  const app = harness(t, { upload: () => ++uploads === 1
    ? { status: 400, result: { error: "Image decoding failed; your note is unchanged." } }
    : { asset: app.media("attachments/images/imported.png"), workspace: app.workspace } });
  await app.open(); app.mode("source"); const original = app.view().state.doc.toString();
  app.view().dispatch({ selection: { anchor: 2, head: 5 } }); app.paste();
  await until(() => app.element("workspace-message").textContent.includes("decoding failed"), "failed import explanation");
  assert.equal(app.view().state.doc.toString(), original);
  assert.deepEqual([app.view().state.selection.main.from, app.view().state.selection.main.to], [2, 5]);
  const input = app.window.document.querySelector('input[type="file"][accept="image/png,image/jpeg,image/webp"]');
  Object.defineProperty(input, "files", { value: [app.file()] });
  input.dispatchEvent(new app.window.Event("change", { bubbles: true }));
  await until(() => app.view().state.doc.toString().includes("!["), "retry through file chooser");
  assert.equal(uploads, 2);
  assert.equal(app.window.WorkspaceKit.undo(app.view()), true);
  assert.equal(app.view().state.doc.toString(), original);
});

test("file drop imports into an actual CodeMirror buffer without saving automatically", async t => {
  const app = harness(t); await app.open(); app.mode("source");
  const original = app.view().state.doc.toString();
  const event = new app.window.MouseEvent("drop", { bubbles: true, cancelable: true, clientX: 10, clientY: 10 });
  Object.defineProperty(event, "dataTransfer", { value: { types: ["Files"], files: [app.file()] } });
  app.view().contentDOM.dispatchEvent(event); assert.equal(event.defaultPrevented, true);
  await until(() => app.view().state.doc.toString().includes("!["), "drop insertion");
  assert.equal(app.disk.text, original);
  assert.equal(app.window.WorkspaceKit.undo(app.view()), true);
  assert.equal(app.view().state.doc.toString(), original);
});

test("a partially failed multi-image import never inserts a partial reference transaction", async t => {
  let uploads = 0;
  const app = harness(t, { upload: () => ++uploads === 1
    ? { asset: app.media("attachments/images/retained.png"), workspace: app.workspace }
    : { status: 400, result: { error: "Second image is unsupported. The first asset is retained." } } });
  await app.open(); app.mode("source"); const original = app.view().state.doc.toString();
  const input = app.window.document.querySelector('input[type="file"][accept="image/png,image/jpeg,image/webp"]');
  Object.defineProperty(input, "files", { value: [app.file(), app.file()] });
  input.dispatchEvent(new app.window.Event("change", { bubbles: true }));
  await until(() => app.element("workspace-message").textContent.includes("Second image"), "partial import failure");
  assert.equal(uploads, 2);
  assert.equal(app.view().state.doc.toString(), original);
  assert.equal(app.disk.text, original);
  assert.equal(app.window.WorkspaceKit.undo(app.view()), false, "failed import adds no editor history entry");
});
