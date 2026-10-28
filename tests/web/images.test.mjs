import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";
import { buildLinkIndex, parseDocument, parseLibraryDocument, graphData, renderIndexedMarkdown } from "../../tools/workspace_links.js";
import { createImageElement, createImageViewer, hydrateImages, imagePlaceholder, isImageDestination, parseImageReference } from "../../tools/workspace_images.js";

const note = (path, text = "") => ({ path, text, revision: "r1", editable: true });
const dom = html => new JSDOM(html, { url: "http://127.0.0.1:12345/" });

test("image syntax retains exact source ranges and dimensions without note nodes or missing targets", () => {
  const text = "[[Target]] ![[../images/图像.png|320x200]] ![Caption](<../images/space name.webp>) [download](../images/a.jpg) [[images/a.png|diagram]] ![remote](https://example.invalid/t.png) ![[unsupported.svg]]";
  const index = buildLinkIndex([note("Notes/Start.md", text), note("Target.md")]);
  const parsed = index.documents.get("Notes/Start.md");
  assert.deepEqual(parsed.references.map(ref => ref.destination), ["Target"]);
  assert.equal(parsed.assets.length, 6); assert.equal(parsed.images.length, 4);
  assert.equal(parsed.images[0].width, 320); assert.equal(parsed.images[0].height, 200);
  assert.equal(text.slice(parsed.images[0].from, parsed.images[0].to), "![[../images/图像.png|320x200]]");
  assert.equal(parsed.images[1].destination, "../images/space%20name.webp");
  assert.equal(parsed.images[2].status, "remote"); assert.equal(parsed.images[3].status, "unsupported");
  assert.equal(parsed.assets[2].destinationRaw, "../images/a.jpg");
  const graph = graphData(index);
  assert.equal(graph.nodes.length, 2); assert.equal(graph.edges.length, 1); assert.equal(graph.unresolved.missing, 0);
  assert.equal(parseLibraryDocument(note("Notes/Start.md", text)).images.length, 4);
});

test("code, comments, frontmatter and escaped embeds cannot become images", () => {
  const parsed = parseDocument("Start.md", "---\nimage: '![[secret.png]]'\n---\n`![[code.png]]` \\![[escaped.png]]\n<!-- ![[comment.png]] -->\n\n```md\n![example](code.png)\n```\n\n![[actual.png]]");
  assert.deepEqual(parsed.images.map(ref => ref.destination), ["actual.png"]);
  assert.deepEqual(parsed.references, []);
});

test("reference-style images preserve definitions and normal note embeds remain note embeds", () => {
  const text = "![Figure][plot]\n\n[plot]: <assets/diagram space.png>\n\n![[Target]] ![note](Target.md)";
  const parsed = parseDocument("Start.md", text);
  assert.equal(parsed.images.length, 1);
  assert.equal(parsed.images[0].definition, "PLOT");
  assert.equal(text.slice(parsed.images[0].destinationFrom, parsed.images[0].destinationTo), "assets/diagram space.png");
  assert.deepEqual(parsed.references.map(ref => ref.destination), ["Target", "Target.md"]);
});

test("image destinations and dimensions are bounded and unsupported references remain inert", () => {
  assert.equal(isImageDestination("path/图像.webp"), true); assert.equal(isImageDestination("Note.md"), false);
  const ref = parseImageReference("../images/pic.png", "250", { sourcePath: "notes/A.md" });
  assert.equal(ref.width, 250); assert.equal(ref.label, "pic.png"); assert.equal(ref.status, "ready");
  assert.equal(parseImageReference("pic.png", "10001x2").width, undefined);
  for (const target of ["file:///tmp/private.png", "javascript:evil.png", "data:image/png;base64,abc", "/tmp/file.png", "\\server\\a.png", "bad%00.png", "bad%zz.png"]) assert.equal(parseImageReference(target).status, "invalid", target);
  for (const target of ["a.svg", "a.gif", "a.apng", "a.avif", "a.png?token=secret"]) assert.equal(parseImageReference(target).status, "unsupported", target);
  const document = dom(imagePlaceholder(parseImageReference("https://tracker.invalid/a.png", "Private figure"))).window.document;
  assert.equal(document.querySelectorAll("img,a[href]").length, 0);
  assert.match(document.body.textContent, /Remote images are not loaded/);
});

test("Reading image placeholders carry local source identity through embedded notes and escape labels", () => {
  const index = buildLinkIndex([note("A.md", "![[nested/B]]"), note("nested/B.md", "![<script>](../assets/图像.png)\n\n[picture](../assets/图像.png)")]);
  const document = dom(renderIndexedMarkdown(index, "A.md")).window.document;
  const refs = [...document.querySelectorAll("[data-image-reference]")].map(node => JSON.parse(node.dataset.imageReference));
  assert.equal(refs.length, 2); assert.ok(refs.every(ref => ref.sourcePath === "nested/B.md"));
  assert.equal(refs[0].label, "<script>");
  assert.equal(document.querySelectorAll("script,img").length, 0);
});

test("hydration uses only resolved same-origin media and preserves an exact annotation action", async () => {
  const uri = "zotero://open-pdf/library/items/PDFKEY01?page=3&annotation=ANNOT001";
  const ref = parseImageReference("../attachments/zotero/crop.png", "Figure", { sourcePath: "notes/A.md", width: 300 });
  const window = dom(`<ul><li><a data-source-uri="${uri.replaceAll("&", "&amp;")}">Area · p. 10</a>${imagePlaceholder(ref)}</li></ul>`).window;
  const seen = [], opened = [], sources = [];
  await hydrateImages(window.document.body, { resolve: async reference => { seen.push(reference); return { url: "/api/workspace/media/opaque", path: "attachments/zotero/crop.png" }; }, onOpen: data => opened.push(data), onSource: uri => sources.push(uri) });
  assert.equal(seen.length, 1); assert.equal(seen[0].sourcePath, "notes/A.md");
  assert.equal(window.document.querySelector("img").src, "http://127.0.0.1:12345/api/workspace/media/opaque");
  window.document.querySelector(".markdown-image-open").click();
  assert.equal(opened[0].sourceURI, uri);
  window.document.querySelector(".markdown-image-source").click(); assert.deepEqual(sources, [uri]);
  const node = createImageElement(ref, { document: window.document, resolve: async () => ({ url: "https://tracker.invalid/a.png" }) });
  await node.imageReady;
  assert.equal(node.querySelector("img"), null); assert.match(node.textContent, /Only workspace image URLs/);
  window.close();
});

test("each multi-page annotation image opens its own physical page in the viewer", async () => {
  const first = "zotero://open-pdf/groups/42/items/PDFKEY01?page=1&annotation=ANNOT001";
  const second = "zotero://open-pdf/groups/42/items/PDFKEY01?page=2&annotation=ANNOT001";
  const text = `- [Highlight · p. 458](${first})\n\n  ![First page](../attachments/zotero/first.png)\n\n  [Open in PDF](${first})\n\n  ![Second page](../attachments/zotero/second.png)\n\n  [Open in PDF](${second})\n`;
  const index = buildLinkIndex([note("notes/A.md", text)]), window = dom(renderIndexedMarkdown(index, "notes/A.md")).window;
  const opened = [], sources = [], viewer = createImageViewer({ document: window.document, onSource: uri => sources.push(uri) });
  await hydrateImages(window.document.body, { resolve: async ref => ({ url: `/api/workspace/media/${ref.label.replaceAll(" ", "-")}` }),
    onOpen: data => { opened.push(data.sourceURI); viewer.open(data); }, onSource: uri => sources.push(uri) });
  const images = [...window.document.querySelectorAll(".markdown-image-open")];
  images[0].click(); viewer.element.querySelector("button").click();
  images[1].click(); viewer.element.querySelector("button").click();
  assert.deepEqual(opened, [first, second]); assert.deepEqual(sources, [first, second]);
  const inline = [...window.document.querySelectorAll(".markdown-image-source")];
  inline[0].click(); inline[1].click(); assert.deepEqual(sources, [first, second, first, second]);
  window.close();
});

test("a page-specific image link cannot cross annotation identity and explicit live-preview pointers survive", async () => {
  const header = "zotero://open-pdf/library/items/PDFKEY01?page=1&annotation=ANNOT001";
  const foreign = "zotero://open-pdf/library/items/PDFKEY01?page=2&annotation=ANNOT002";
  const ref = parseImageReference("crop.png", "Figure");
  const window = dom(`<li><a data-source-uri="${header.replaceAll("&", "&amp;")}">Header</a><p>${imagePlaceholder(ref)}</p><p><a data-source-uri="${foreign.replaceAll("&", "&amp;")}">Open in PDF</a></p></li>`).window;
  const opened = [];
  await hydrateImages(window.document.body, { resolve: async () => ({ url: "/api/workspace/media/crop" }), onOpen: data => opened.push(data), onSource: () => {} });
  window.document.querySelector(".markdown-image-open").click(); assert.equal(opened[0].sourceURI, null);
  assert.equal(window.document.querySelector(".markdown-image-source"), null);
  const explicit = createImageElement({ ...ref, sourceURI: header }, { document: window.document, resolve: async () => ({ url: "/api/workspace/media/crop" }), onOpen: data => opened.push(data) });
  await explicit.imageReady; explicit.querySelector("button").click(); assert.equal(opened[1].sourceURI, header);
  window.close();
});

test("resolver failures and cancelled widgets never replace a draft or create an image request", async () => {
  const window = dom("").window;
  const ref = parseImageReference("missing.png", "Missing");
  const failed = createImageElement(ref, { document: window.document, resolve: async () => { throw new Error("Image file is missing."); } });
  await failed.imageReady; assert.match(failed.textContent, /Image file is missing/); assert.equal(failed.querySelector("img"), null);
  let finish;
  const cancelled = createImageElement(ref, { document: window.document, resolve: () => new Promise(resolve => { finish = resolve; }) });
  cancelled.cancelImageLoad(); finish({ url: "/api/workspace/media/test" }); await cancelled.imageReady;
  assert.equal(cancelled.querySelector("img"), null);
  let calls = 0;
  const unsupported = createImageElement(parseImageReference("a.svg"), { document: window.document, resolve: () => { calls += 1; } });
  await unsupported.imageReady; assert.equal(calls, 0);
  window.close();
});

test("larger image viewer has close, backdrop, Escape and focus restoration without external URLs", () => {
  const window = dom('<button id="origin">Open</button>').window, document = window.document;
  const origin = document.getElementById("origin"); origin.focus();
  const viewer = createImageViewer({ document });
  assert.throws(() => viewer.open({ url: "https://outside.invalid/a.png" }), /Only workspace/);
  const data = { url: "/api/workspace/media/test", label: "Diagram" };
  viewer.open(data); assert.equal(viewer.element.getAttribute("role"), "dialog");
  assert.equal(document.activeElement.getAttribute("aria-label"), "Close image viewer");
  document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true })); assert.equal(viewer.element, null); assert.equal(document.activeElement, origin);
  viewer.open(data).click(); assert.equal(viewer.element, null);
  viewer.open(data).querySelector("button").click(); assert.equal(viewer.element, null);
  window.close();
});
